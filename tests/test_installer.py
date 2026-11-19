import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from installer.docker import DockerError
from installer.envconfig import DefaultEnvConfigurator
from installer.envfile import read_env
from installer.errors import TelepitoError, UserAbort
from installer.health import HealthResponse
from installer.installer import (
    DatabaseOps,
    DatabaseSetup,
    Deps,
    Installer,
    InstallFailed,
    InstallJob,
    ReleaseError,
    ReleaseMeta,
    parse_migrations,
    parse_sums,
)
from installer.logging_ import Logger
from installer.manifest import Manifest
from installer.plan import PlanKind, PlanRequest, build_plan
from installer.state import (
    DatabaseInfo,
    inspect_target,
    load_install_info,
    load_installs,
)
from installer.ui.base import UiBack
from installer.ui.scripted import ScriptedUi
from installer.versions import Version

from .fake_docker import FakeDocker
from .fake_release import ReleaseSpec

REPO = "o/investment"
DB_INFO = DatabaseInfo(
    "database-postgres-1", "shared-infra", "postgres", "investment", "investment"
)


class FakeDb(DatabaseOps):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.backup_result: Path | None = None
        self.write_backups = True
        self.proceed_without_backup = False  # the user's answer when a backup fails

    def precheck(self, manifest: Manifest) -> list[str]:
        self.calls.append("precheck")
        return []

    def create(self, manifest: Manifest) -> DatabaseSetup:
        self.calls.append("create")
        return DatabaseSetup(DB_INFO, {"DATABASE_URL": "postgresql://investment:pw@postgres/x"})

    def backup(self, info: DatabaseInfo, version: str, backups_dir: Path) -> Path | None:
        self.calls.append(f"backup:{version}")
        if not self.write_backups:
            if self.proceed_without_backup:
                return None
            raise UserAbort("Mentés nélkül a művelet megáll.")
        path = backups_dir / f"investment-{version}-20261119T000000Z.dump"
        path.write_bytes(b"dump")
        self.backup_result = path
        return path

    def restore(self, info: DatabaseInfo, backup: Path, prepare: Any = None) -> None:
        self.calls.append(f"restore:{backup.name}")
        if prepare is not None:
            prepare()

    def rotate_password(self, manifest: Manifest, info: DatabaseInfo, env_path: Path) -> None:
        self.calls.append("rotate")


class InstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_dir = self.root / "telepito"
        self.target = self.root / "investment"
        self.target.mkdir()
        self.meta_dir = self.root / "meta"

        from .fake_release import MemoryGitHub

        self.github = MemoryGitHub()
        self.docker = FakeDocker()
        self.docker.archive_images = []
        self.ui = ScriptedUi()
        self.log = Logger(None)
        self.db = FakeDb()
        self.healthy_version: str | None = None  # what the "application" reports
        self.clock_value = 0.0
        self.deps = Deps(
            github=self.github,
            docker=self.docker,
            ui=self.ui,
            log=self.log,
            state_dir=self.state_dir,
            env=DefaultEnvConfigurator(self.log.redactor),
            db=self.db,
            health=self.fake_health,
            port_in_use=lambda port: False,
            disk_free=lambda path: 100 * 2**30,
            sleep=self.fake_sleep,
            monotonic=lambda: self.clock_value,
        )
        self.installer = Installer(self.deps)

    # fake application ----------------------------------------------------------------

    def fake_health(self, url: str) -> HealthResponse:
        if self.healthy_version is None:
            raise OSError("connection refused")
        return HealthResponse(
            200, json.dumps({"status": "ok", "version": self.healthy_version}).encode()
        )

    def fake_sleep(self, seconds: float) -> None:
        self.clock_value += seconds

    def app_follows_compose(self) -> None:
        """Make the fake application report whatever APP_VERSION compose was started with."""

        def on_up(project: str, compose: Path, env: Path) -> None:
            self.healthy_version = read_env(env)["APP_VERSION"]

        self.docker.on_up = on_up

    # helpers ---------------------------------------------------------------------------

    def publish(self, version: str, **kwargs: Any) -> tuple[ReleaseSpec, Any]:
        migrations = kwargs.pop("migrations", ("m1",))
        spec = ReleaseSpec(version, migrations=migrations)
        for key, value in kwargs.pop("manifest_edits", {}).items():
            spec.manifest[key] = value
        spec.compose = kwargs.pop("compose", spec.compose)
        release = self.github.add(spec, **kwargs)
        self.docker.archive_images = [f"investment:{version}"]
        return spec, release

    def make_job(self, release: Any, *, port: int = 2570) -> InstallJob:
        meta = self.installer.fetch_metadata(REPO, release, self.meta_dir / release.tag)
        version = Version.parse(release.tag)
        plan = build_plan(
            PlanRequest(
                repo=REPO,
                version=version,
                target=str(self.target),
                manifest=meta.manifest,
                target_state=inspect_target(self.target),
                requested_migrations=meta.migrations,
            )
        )
        return InstallJob(REPO, release, meta, plan, self.target, port)

    def install(self, version: str, **kwargs: Any) -> Any:
        spec, release = self.publish(version, **kwargs)
        self.app_follows_compose()
        return self.installer.install(self.make_job(release))

    def env(self) -> dict[str, str]:
        return read_env(self.target / "shared" / ".env.production")


class HelperTests(unittest.TestCase):
    def test_parse_sums(self) -> None:
        h = "a" * 64
        self.assertEqual(
            parse_sums(f"{h}  deploy.json\n{h.upper()} *image.tar.gz\n\n"),
            {"deploy.json": h, "image.tar.gz": h},
        )

    def test_parse_sums_rejects_bad_input(self) -> None:
        for text in ["nothex  file", f"{'a' * 64}  ../evil", f"{'a' * 64}  a/b"]:
            with self.subTest(text=text), self.assertRaises(ReleaseError):
                parse_sums(text)

    def test_parse_migrations(self) -> None:
        self.assertEqual(
            parse_migrations("20260101_init\n\n 20260202_x \n"), ("20260101_init", "20260202_x")
        )
        with self.assertRaises(ReleaseError):
            parse_migrations("ok\n../bad\n")


class MetadataTests(InstallerTestCase):
    def test_fetch_and_verify(self) -> None:
        _, release = self.publish("0.1.0", migrations=("m1", "m2"))
        meta = self.installer.fetch_metadata(REPO, release, self.meta_dir)
        self.assertEqual(meta.manifest.name, "investment")
        self.assertEqual(meta.migrations, ("m1", "m2"))
        self.assertNotIn("image.tar.gz", self.github.downloads)  # the big file waits

    def test_missing_asset(self) -> None:
        _, release = self.publish("0.1.0", omit=("migrations.txt",))
        with self.assertRaises(ReleaseError) as ctx:
            self.installer.fetch_metadata(REPO, release, self.meta_dir)
        self.assertIn("migrations.txt", str(ctx.exception))

    def test_tampered_file_is_rejected_and_deleted(self) -> None:
        _, release = self.publish("0.1.0", tamper="deploy.json")
        with self.assertRaises(ReleaseError):
            self.installer.fetch_metadata(REPO, release, self.meta_dir)
        self.assertFalse((self.meta_dir / "deploy.json").exists())

    def test_tampered_compose_is_rejected(self) -> None:
        _, release = self.publish("0.1.0", tamper="docker-compose.deploy.yml")
        with self.assertRaises(ReleaseError):
            self.installer.fetch_metadata(REPO, release, self.meta_dir)


class PreflightTests(InstallerTestCase):
    def meta(self, version: str = "0.1.0") -> tuple[ReleaseMeta, Any]:
        _, release = self.publish(version)
        return self.installer.fetch_metadata(REPO, release, self.meta_dir), release

    def test_clean(self) -> None:
        meta, release = self.meta()
        self.assertEqual(
            self.installer.preflight(meta, release, self.target, kind=PlanKind.NEW, port=2570), []
        )
        self.assertIn("precheck", self.db.calls)

    def test_not_enough_space(self) -> None:
        meta, release = self.meta()
        self.deps.disk_free = lambda path: 10
        problems = self.installer.preflight(
            meta, release, self.target, kind=PlanKind.NEW, port=2570
        )
        self.assertEqual(len(problems), 2)  # target and docker root
        self.assertTrue(all("szabad hely" in p for p in problems))

    def test_port_in_use_only_matters_for_new_installs(self) -> None:
        meta, release = self.meta()
        self.deps.port_in_use = lambda port: True
        new = self.installer.preflight(meta, release, self.target, kind=PlanKind.NEW, port=2570)
        update = self.installer.preflight(
            meta, release, self.target, kind=PlanKind.UPDATE, port=2570
        )
        self.assertTrue(any("foglalt" in p for p in new))
        self.assertEqual(update, [])

    def test_docker_unavailable(self) -> None:
        meta, release = self.meta()
        self.docker.fail.add("info")
        problems = self.installer.preflight(
            meta, release, self.target, kind=PlanKind.NEW, port=2570
        )
        self.assertTrue(any("fake info" in p for p in problems))


class NewInstallTests(InstallerTestCase):
    def test_full_new_installation(self) -> None:
        result = self.install("0.1.0")
        t = self.target
        self.assertEqual(result.version, "0.1.0")
        self.assertIn("127.0.0.1:2570", result.proxy_snippet)
        self.assertEqual(result.post_install, ("Hozz létre felhasználót",))
        # layout
        self.assertEqual((t / "current").resolve(), (t / "releases" / "0.1.0").resolve())
        self.assertTrue((t / "releases" / "0.1.0" / "docker-compose.deploy.yml").is_file())
        self.assertTrue((t / "releases" / "0.1.0" / ".env.production").is_symlink())
        self.assertFalse(list((t / "releases").glob(".tmp-*")))
        self.assertFalse((t / "releases" / "0.1.0" / "image.tar.gz").exists())
        # env
        env = self.env()
        self.assertEqual(env["NODE_ENV"], "production")
        self.assertEqual(env["BASE_PATH"], "/investment/")
        self.assertGreaterEqual(len(env["APIKEY_MASTER_SECRET"]), 48)
        self.assertTrue(env["DATABASE_URL"].startswith("postgresql://"))
        compose_env = read_env(t / "shared" / "compose.env")
        self.assertEqual(
            compose_env, {"APP_VERSION": "0.1.0", "PORT": "2570", "DB_NETWORK": "shared-infra"}
        )
        # state
        info = load_install_info(t)
        assert info is not None
        self.assertEqual((info.version, info.migrations, info.port), ("0.1.0", ("m1",), 2570))
        self.assertEqual(info.database, DB_INFO)
        records = load_installs(self.state_dir / "installs.json")
        self.assertEqual([(r.name, r.version) for r in records], [("investment", "0.1.0")])
        self.assertEqual(self.db.calls, ["create"])

    def test_secrets_never_reach_the_log(self) -> None:
        self.install("0.1.0")
        secret = self.env()["APIKEY_MASTER_SECRET"]
        self.assertNotIn(secret, "\n".join(self.log.records))
        self.assertNotIn(secret, self.log.redactor.redact(secret))

    def test_image_archive_removed_and_image_loaded_once(self) -> None:
        self.install("0.1.0")
        self.assertEqual(self.docker.names().count("load_image"), 1)
        self.assertIn("investment:0.1.0", self.docker.images)

    def test_compose_started_with_release_dir_and_project(self) -> None:
        self.install("0.1.0")
        (project, compose, env_file, version) = self.docker.up_log[0]
        self.assertEqual(project, "investment")
        self.assertEqual(compose, self.target / "releases" / "0.1.0" / "docker-compose.deploy.yml")
        self.assertEqual(env_file, self.target / "shared" / "compose.env")
        self.assertEqual(version, "0.1.0")

    def test_image_with_wrong_tag_is_rejected(self) -> None:
        _, release = self.publish("0.1.0")
        self.docker.archive_images = ["investment:9.9.9"]
        with self.assertRaises(ReleaseError):
            self.installer.install(self.make_job(release))
        self.assertIsNone(load_install_info(self.target))
        self.assertFalse((self.target / "current").exists())

    def test_build_in_compose_is_blocked_before_anything_is_started(self) -> None:
        _, release = self.publish("0.1.0", compose="services:\n  app:\n    build: .\n")
        with self.assertRaises(ReleaseError) as ctx:
            self.installer.install(self.make_job(release))
        self.assertIn("build", str(ctx.exception))
        self.assertNotIn("compose_up", self.docker.names())

    def test_missing_target_directory(self) -> None:
        _, release = self.publish("0.1.0")
        job = self.make_job(release)
        self.target.rmdir()
        with self.assertRaises(TelepitoError) as ctx:
            self.installer.install(job)
        self.assertIn("sudo", str(ctx.exception))

    def test_blocked_plan_is_refused(self) -> None:
        (self.target / "stranger.txt").write_text("x", encoding="utf-8")
        _, release = self.publish("0.1.0")
        with self.assertRaises(TelepitoError):
            self.installer.install(self.make_job(release))

    def test_no_database_application(self) -> None:
        edits = {"database": {"required": False}}
        self.install("0.1.0", manifest_edits=edits)
        self.assertEqual(self.db.calls, [])
        self.assertEqual(read_env(self.target / "shared" / "compose.env").get("DB_NETWORK"), None)


class UpdateTests(InstallerTestCase):
    def test_update_backs_up_and_switches(self) -> None:
        self.install("0.1.0")
        secret = self.env()["APIKEY_MASTER_SECRET"]
        result = self.install("0.2.0", migrations=("m1", "m2"))
        self.assertEqual(result.version, "0.2.0")
        self.assertEqual(self.db.calls, ["create", "backup:0.1.0"])
        self.assertEqual(result.backup_path, self.db.backup_result)
        t = self.target
        self.assertEqual((t / "current").resolve(), (t / "releases" / "0.2.0").resolve())
        info = load_install_info(t)
        assert info is not None
        self.assertEqual((info.version, info.previous_versions), ("0.2.0", ("0.1.0",)))
        self.assertEqual(info.migrations, ("m1", "m2"))
        self.assertEqual(self.env()["APIKEY_MASTER_SECRET"], secret)  # untouched
        self.assertEqual(
            [r.version for r in load_installs(self.state_dir / "installs.json")], ["0.2.0"]
        )

    def test_update_only_asks_for_new_variables_and_removes_dropped_ones(self) -> None:
        self.install("0.1.0")
        env_path = self.target / "shared" / ".env.production"
        env_path.write_text(
            env_path.read_text(encoding="utf-8") + "MY_OWN='keep me'\n", encoding="utf-8"
        )
        edits_env = [
            {"name": "NODE_ENV", "fixed": "production"},
            {"name": "NEW_FLAG", "default": "on"},
        ]
        self.install("0.2.0", manifest_edits={"env": edits_env})
        env = self.env()
        self.assertEqual(env["NEW_FLAG"], "on")
        self.assertEqual(env["MY_OWN"], "keep me")  # foreign lines survive
        self.assertNotIn("BASE_PATH", env)  # declared before, dropped now
        self.assertIn("DATABASE_URL", env)  # set by the database step, not declared in env[]

    def test_backup_failure_stops_the_update(self) -> None:
        self.install("0.1.0")
        self.db.write_backups = False
        with self.assertRaises(UserAbort):
            self.install("0.2.0")
        self.assertEqual(load_install_info(self.target).version, "0.1.0")  # type: ignore[union-attr]
        self.assertEqual(self.docker.names().count("compose_up"), 1)

    def test_update_may_continue_without_a_backup_when_the_user_insists(self) -> None:
        self.install("0.1.0")
        self.db.write_backups = False
        self.db.proceed_without_backup = True
        result = self.install("0.2.0")
        self.assertIsNone(result.backup_path)
        self.assertEqual(load_install_info(self.target).version, "0.2.0")  # type: ignore[union-attr]

    def test_old_backups_are_offered_for_deletion_and_the_choice_is_remembered(self) -> None:
        self.install("0.1.0")
        old = self.target / "backups" / "investment-0.0.1-20240101T000000Z.dump"
        old.write_bytes(b"x" * 2048)
        long_ago = 1_600_000_000  # September 2020
        os.utime(old, (long_ago, long_ago))
        self.ui.push("always")  # delete now, and always from now on
        self.install("0.2.0")
        self.assertFalse(old.exists())
        info = load_install_info(self.target)
        assert info is not None
        self.assertTrue(info.auto_delete_old_backups)
        # next update: deleted silently, no question
        again = self.target / "backups" / "investment-0.0.2-20240201T000000Z.dump"
        again.write_bytes(b"y")
        os.utime(again, (long_ago, long_ago))
        before = len([c for c in self.ui.calls if c.kind == "menu"])
        self.install("0.3.0")
        self.assertFalse(again.exists())
        self.assertEqual(len([c for c in self.ui.calls if c.kind == "menu"]), before)

    def test_compose_changes_are_shown_and_dangerous_ones_confirmed(self) -> None:
        self.install("0.1.0")
        dangerous = ReleaseSpec("x").compose + "    privileged: true\n"
        self.ui.push(False)  # decline the dangerous change
        with self.assertRaises(UserAbort):
            self.install("0.2.0", compose=dangerous)
        self.assertTrue(any("!!" in c.text for c in self.ui.calls if c.kind == "message"))
        self.assertEqual(load_install_info(self.target).version, "0.1.0")  # type: ignore[union-attr]

    def test_compose_changes_accepted(self) -> None:
        self.install("0.1.0")
        dangerous = ReleaseSpec("x").compose + "    privileged: true\n"
        self.ui.push(True)
        self.install("0.2.0", compose=dangerous)
        self.assertEqual(load_install_info(self.target).version, "0.2.0")  # type: ignore[union-attr]

    def test_old_releases_and_images_are_cleaned_by_keep(self) -> None:
        # keep = 2 in the sample manifest: the running one plus two previous ones remain.
        for version in ["0.1.0", "0.2.0", "0.3.0", "0.4.0"]:
            self.install(version)
        releases = sorted(p.name for p in (self.target / "releases").iterdir())
        self.assertEqual(releases, ["0.2.0", "0.3.0", "0.4.0"])
        self.assertNotIn("investment:0.1.0", self.docker.images)
        self.assertIn("investment:0.3.0", self.docker.images)


class SameVersionTests(InstallerTestCase):
    def test_reinstall_same_version(self) -> None:
        self.install("0.1.0")
        result = self.install("0.1.0")
        self.assertEqual(result.version, "0.1.0")
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.previous_versions, ())
        self.assertTrue((self.target / "releases" / "0.1.0").is_dir())


class FailureTests(InstallerTestCase):
    def test_new_install_failure_keep(self) -> None:
        _, release = self.publish("0.1.0")
        self.healthy_version = None  # never becomes healthy
        self.ui.push("keep")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        self.assertFalse((self.target / "current").exists())
        self.assertIsNone(load_install_info(self.target))
        self.assertEqual(load_installs(self.state_dir / "installs.json"), [])
        self.assertTrue(any("naplója" in m for m in self.ui.messages()))

    def test_new_install_failure_stop(self) -> None:
        _, release = self.publish("0.1.0")
        self.ui.push("stop")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        self.assertIn("compose_stop", self.docker.names())

    def test_escape_in_recovery_menu_keeps_everything(self) -> None:
        _, release = self.publish("0.1.0")
        self.ui.push(UiBack)
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        self.assertNotIn("compose_stop", self.docker.names())

    def test_wrong_version_reported_is_unhealthy(self) -> None:
        _, release = self.publish("0.1.0")
        self.healthy_version = "0.0.9"  # an old container answers on the port
        self.ui.push("keep")
        with self.assertRaises(InstallFailed) as ctx:
            self.installer.install(self.make_job(release))
        self.assertIn("időtúllépés", str(ctx.exception))

    def test_compose_up_failure_is_a_failure_not_a_crash(self) -> None:
        _, release = self.publish("0.1.0")
        self.docker.fail.add("compose_up")
        self.ui.push("keep")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))

    def test_update_failure_offers_and_performs_rollback(self) -> None:
        self.install("0.1.0")
        spec, release = self.publish("0.2.0")  # same migrations: DB compatible
        self.healthy_version = None
        calls = {"n": 0}

        def on_up(project: str, compose: Path, env: Path) -> None:
            calls["n"] += 1
            # the new release never becomes healthy; the rolled back one does
            version = read_env(env)["APP_VERSION"]
            self.healthy_version = version if version == "0.1.0" else None

        self.docker.on_up = on_up
        self.ui.push("rollback")
        with self.assertRaises(InstallFailed) as ctx:
            self.installer.install(self.make_job(release))
        self.assertIn("visszaállítva és fut", str(ctx.exception))
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.1.0")  # state unchanged
        self.assertEqual(
            (self.target / "current").resolve(), (self.target / "releases" / "0.1.0").resolve()
        )
        self.assertEqual(read_env(self.target / "shared" / "compose.env")["APP_VERSION"], "0.1.0")
        self.assertEqual(calls["n"], 2)

    def test_rollback_not_offered_when_database_moved_ahead(self) -> None:
        self.install("0.1.0")
        _, release = self.publish("0.2.0", migrations=("m1", "m2"))
        self.healthy_version = None
        self.docker.on_up = lambda *a: None
        self.ui.push("keep")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        menu = [c for c in self.ui.calls if c.kind == "menu"][-1]
        self.assertNotIn("rollback", menu.items)
        self.assertIn("rollback_restore", menu.items)  # a backup exists

    def test_rollback_with_database_restore(self) -> None:
        self.install("0.1.0")
        _, release = self.publish("0.2.0", migrations=("m1", "m2"))
        self.healthy_version = None

        def on_up(project: str, compose: Path, env: Path) -> None:
            version = read_env(env)["APP_VERSION"]
            self.healthy_version = version if version == "0.1.0" else None

        self.docker.on_up = on_up
        self.ui.push("rollback_restore")
        with self.assertRaises(InstallFailed) as ctx:
            self.installer.install(self.make_job(release))
        self.assertIn("visszaállítva", str(ctx.exception))
        self.assertTrue(any(c.startswith("restore:") for c in self.db.calls))

    def test_ctrl_c_leaves_state_untouched(self) -> None:
        _, release = self.publish("0.1.0")
        job = self.make_job(release)

        def interrupt(project: str, compose: Path, env: Path) -> None:
            raise KeyboardInterrupt

        self.docker.on_up = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.installer.install(job)
        self.assertFalse((self.target / "current").exists())
        self.assertIsNone(load_install_info(self.target))
        # the lock was released: a new run can start
        self.docker.on_up = None
        self.app_follows_compose()
        self.assertEqual(self.installer.install(job).version, "0.1.0")

    def test_concurrent_run_is_refused(self) -> None:
        from installer.lock import FileLock, LockBusy

        _, release = self.publish("0.1.0")
        job = self.make_job(release)
        self.state_dir.mkdir(parents=True)
        with FileLock(self.state_dir / ".lock"), self.assertRaises(LockBusy):
            self.installer.install(job)


class RollbackToTests(InstallerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install("0.1.0")
        self.install("0.2.0", migrations=("m1",))

    def test_lists_only_restorable_versions(self) -> None:
        self.assertEqual(self.installer.previous_versions(self.target), ["0.1.0"])
        self.docker.images.discard("investment:0.1.0")
        self.assertEqual(self.installer.previous_versions(self.target), [])

    def test_rollback_to_previous_version(self) -> None:
        self.ui.push("VISSZALÉPÉS")
        result = self.installer.rollback_to(self.target, "0.1.0")
        self.assertEqual(result.version, "0.1.0")
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual((info.version, info.previous_versions), ("0.1.0", ("0.2.0",)))
        self.assertEqual(
            (self.target / "current").resolve(), (self.target / "releases" / "0.1.0").resolve()
        )
        self.assertEqual(self.db.calls[-1], "backup:0.2.0")

    def test_wrong_confirmation_word_aborts_without_changes(self) -> None:
        self.ui.push("igen")
        with self.assertRaises(UserAbort):
            self.installer.rollback_to(self.target, "0.1.0")
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.2.0")

    def test_missing_image_is_reported(self) -> None:
        self.docker.images.discard("investment:0.1.0")
        with self.assertRaises(TelepitoError) as ctx:
            self.installer.rollback_to(self.target, "0.1.0")
        self.assertIn("Töltsd le újra", str(ctx.exception))

    def test_unknown_version(self) -> None:
        with self.assertRaises(TelepitoError):
            self.installer.rollback_to(self.target, "9.9.9")

    def test_rollback_blocked_when_database_is_ahead(self) -> None:
        info = load_install_info(self.target)
        assert info is not None
        from dataclasses import replace

        from installer.state import save_install_info

        save_install_info(self.target, replace(info, migrations=("m1", "m_extra")))
        with self.assertRaises(TelepitoError) as ctx:
            self.installer.rollback_to(self.target, "0.1.0")
        self.assertIn("nem végezhető el", str(ctx.exception))


class RestartTests(InstallerTestCase):
    def test_restart_recreates_and_checks_health(self) -> None:
        self.install("0.1.0")
        self.docker.calls.clear()
        self.assertIsNone(self.installer.restart(self.target))
        (name, args) = next(c for c in self.docker.calls if c[0] == "compose_up")
        self.assertEqual(args[0], "investment")
        self.assertTrue(args[3])  # force_recreate

    def test_restart_reports_an_unhealthy_application(self) -> None:
        self.install("0.1.0")
        self.healthy_version = None
        self.docker.on_up = None
        problem = self.installer.restart(self.target)
        assert problem is not None
        self.assertIn("időtúllépés", problem)

    def test_restart_reports_docker_failure(self) -> None:
        self.install("0.1.0")
        self.docker.fail.add("compose_up")
        self.assertIn("fake compose_up", self.installer.restart(self.target) or "")

    def test_restart_without_installation(self) -> None:
        with self.assertRaises(TelepitoError):
            self.installer.restart(self.target)


class DockerErrorTests(InstallerTestCase):
    def test_load_failure_propagates_and_changes_nothing(self) -> None:
        _, release = self.publish("0.1.0")
        self.docker.fail.add("load_image")
        with self.assertRaises(DockerError):
            self.installer.install(self.make_job(release))
        self.assertFalse((self.target / "current").exists())
        self.assertIsNone(load_install_info(self.target))


if __name__ == "__main__":
    unittest.main()
