"""Data volumes through the installer: review, backup before a change, recovery, restore."""

import os
import sys
import tarfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from installer.backup import group_sets, list_all_backups, list_volume_backups
from installer.docker import Docker, ExecResult
from installer.envfile import read_env
from installer.errors import UserAbort
from installer.flows_data import backup_menu, set_key
from installer.installer import InstallFailed, ReleaseError, RestoreChoice
from installer.plan import PlanKind, StepId, format_plan
from installer.state import load_install_info, load_installs
from installer.status import collect_status, format_status
from installer.ui.base import UiBack
from installer.volumebackup import BACKUP_COMMAND, RESTORE_COMMAND, SIZE_COMMAND

from .appsupport import AppTestCase
from .test_installer import InstallerTestCase

SOURCE = "investment_uploads"


def named_mount(target: str = "/app/uploads", source: str = "uploads") -> dict[str, object]:
    return {"type": "volume", "source": source, "target": target}


class VolumeCase(InstallerTestCase):
    """An installer whose release keeps one named volume, ``uploads``."""

    def setUp(self) -> None:
        super().setUp()
        self.mounts: list[dict[str, object]] = [named_mount()]
        self.top: dict[str, object] = {"uploads": {"name": SOURCE}}
        self.docker.config_handler = lambda project, compose, env: {
            "name": project,
            "services": {"app": {"volumes": self.mounts}},
            "volumes": self.top,
        }
        self.edits: dict[str, Any] = {
            "database": {"required": False},
            "volumes": [{"name": "uploads", "mountPath": "/app/uploads"}],
        }
        self.backups = self.target / "backups"

    def first(self, version: str = "0.1.0") -> Any:
        self.ui.push(True)  # a new installation shows the data volumes and asks once
        return self.install(version, manifest_edits=self.edits)

    def update(self, version: str) -> Any:
        return self.install(version, manifest_edits=self.edits)

    def fill(self, **files: bytes) -> None:
        self.docker.volume_data[SOURCE] = dict(files)

    def archive_names(self) -> list[str]:
        return sorted(p.name for p in self.backups.glob("*.tar.gz"))


class ReviewTests(VolumeCase):
    def test_a_new_installation_shows_the_volumes_and_asks(self) -> None:
        self.first()
        text = "\n".join(c.text for c in self.ui.calls if c.kind == "message")
        self.assertIn("uploads (volume) -> /app/uploads, backed up", text)
        self.assertTrue(any(c.kind == "confirm" and "Continue" in c.text for c in self.ui.calls))
        self.assertIsNotNone(load_install_info(self.target))

    def test_declining_stops_before_anything_is_changed(self) -> None:
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.install("0.1.0", manifest_edits=self.edits)
        self.assertIsNone(load_install_info(self.target))
        self.assertNotIn("compose_up", self.docker.names())

    def test_an_installation_without_volumes_asks_nothing_extra(self) -> None:
        self.mounts.clear()
        self.top.clear()
        self.install("0.1.0", manifest_edits={"database": {"required": False}})
        self.assertEqual([c for c in self.ui.calls if c.kind == "confirm"], [])

    def test_risky_lines_of_a_new_compose_are_flagged_and_confirmed(self) -> None:
        self.mounts.clear()
        self.top.clear()
        compose = "services:\n  app:\n    image: x\n    privileged: true\n"
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.install("0.1.0", manifest_edits={"database": {"required": False}}, compose=compose)
        self.assertTrue(any("!! privileged: true" in c.text for c in self.ui.calls))

    def test_unnamed_volumes_are_reported(self) -> None:
        self.mounts.append({"type": "volume", "target": "/cache"})
        self.first()
        text = "\n".join(c.text for c in self.ui.calls if c.kind == "message")
        self.assertIn("Unnamed volumes (NOT backed up", text)
        self.assertIn("/cache", text)

    def test_the_compose_model_is_read_with_the_installers_variables(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(project: str, compose: Path, env: Path) -> dict[str, Any]:
            seen.append(read_env(env))
            assert (compose.parent / ".env.production").exists()  # compose needs the env_file
            return {"services": {"app": {"volumes": self.mounts}}, "volumes": self.top}

        self.docker.config_handler = handler
        self.first()
        self.assertEqual(seen[0]["APP_VERSION"], "0.1.0")
        self.assertIn("DATA_DIR", seen[0])
        self.assertEqual(seen[0]["PORT"], "2570")
        # the throw-away files are gone
        staging_leftovers = list((self.target / "releases").glob("*/.check.env"))
        self.assertEqual(staging_leftovers, [])
        self.assertTrue((self.target / "releases" / "0.1.0" / ".env.production").is_symlink())


class ContractViolationTests(VolumeCase):
    def assert_blocked(self, text: str) -> None:
        with self.assertRaises(ReleaseError) as ctx:
            self.update("0.2.0")
        self.assertIn(text, str(ctx.exception))
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.1.0")  # nothing changed
        self.assertEqual(self.archive_names(), [])
        self.assertNotIn("compose_stop", self.docker.names())

    def test_violations_stop_an_update_before_any_change(self) -> None:
        self.first()
        self.fill(a=b"1")
        self.docker.calls.clear()
        cases: dict[str, tuple[list[dict[str, object]], dict[str, object], str]] = {
            "socket": (
                [named_mount(), {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}],
                {"uploads": {"name": SOURCE}},
                "Docker socket",
            ),
            "host path": (
                [named_mount(), {"type": "bind", "source": "/etc", "target": "/e"}],
                {"uploads": {"name": SOURCE}},
                "outside the data directory",
            ),
            "external": (
                [named_mount()],
                {"uploads": {"name": "uploads", "external": True}},
                "external",
            ),
            "renamed": ([named_mount()], {"uploads": {"name": "other"}}, "custom name"),
            "undeclared": (
                [named_mount(), named_mount("/x", "extra")],
                {"uploads": {"name": SOURCE}, "extra": {"name": "investment_extra"}},
                "does not declare",
            ),
            "missing": ([], {}, "does not mount it"),
        }
        for label, (mounts, top, text) in cases.items():
            with self.subTest(label):
                del self.mounts[:]
                self.top.clear()
                self.mounts.extend(mounts)
                self.top.update(top)
                self.assert_blocked(text)

    def test_an_unusable_compose_file_is_a_release_error(self) -> None:
        self.first()
        self.docker.fail = {"compose_config"}
        with self.assertRaises(ReleaseError):
            self.update("0.2.0")

    def test_a_new_installation_is_blocked_too(self) -> None:
        self.mounts.append({"type": "bind", "source": "/etc", "target": "/e"})
        with self.assertRaises(ReleaseError):
            self.install("0.1.0", manifest_edits=self.edits)
        self.assertIsNone(load_install_info(self.target))


class BackupBeforeUpdateTests(VolumeCase):
    def test_the_volume_is_archived_and_the_application_stopped_meanwhile(self) -> None:
        self.first()
        self.fill(**{"a.txt": b"hello", "b/c.txt": b"world"})
        self.docker.calls.clear()
        result = self.update("0.2.0")
        (archive,) = self.backups.glob("uploads-0.1.0-*.tar.gz")
        self.assertEqual(result.volume_backups, (archive,))
        with tarfile.open(archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ["a.txt", "b/c.txt"])
        order = self.docker.names()
        self.assertLess(order.index("compose_stop"), order.index("run_helper"))
        self.assertLess(order.index("run_helper"), order.index("compose_up"))
        self.assertEqual(order.count("compose_stop"), 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_the_archive_is_private(self) -> None:
        self.first()
        self.update("0.2.0")
        (archive,) = self.backups.glob("*.tar.gz")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_the_helper_runs_the_installed_image_read_only(self) -> None:
        self.first()
        self.update("0.2.0")
        (record,) = self.docker.helper_log
        self.assertEqual(record.image, "investment:0.1.0")  # the image that is already loaded
        self.assertEqual(record.command, BACKUP_COMMAND)
        (mount,) = record.mounts
        self.assertEqual((mount.source, mount.target, mount.read_only), (SOURCE, "/data", True))

    def test_with_a_database_both_belong_to_one_set(self) -> None:
        self.edits.pop("database")  # the sample manifest requires a database
        self.first()
        self.update("0.2.0")
        sets = group_sets(list_all_backups(self.backups, "investment"))
        (only,) = sets
        self.assertEqual(sorted(f.kind for f in only.files), ["db", "volume"])
        self.assertEqual(only.version, "0.1.0")

    def test_no_backup_when_the_release_has_no_volume(self) -> None:
        self.mounts.clear()
        self.top.clear()
        self.install("0.1.0", manifest_edits={"database": {"required": False}})
        self.install("0.2.0", manifest_edits={"database": {"required": False}})
        self.assertEqual(self.docker.helper_log, [])
        self.assertEqual(self.docker.names().count("compose_stop"), 0)

    def test_a_volume_without_backup_is_left_alone(self) -> None:
        self.edits["volumes"][0]["backup"] = False
        self.first()
        self.docker.calls.clear()
        self.update("0.2.0")
        self.assertEqual(self.docker.helper_log, [])
        self.assertNotIn("compose_stop", self.docker.names())

    def test_an_aborted_backup_starts_the_application_again(self) -> None:
        self.first()
        self.docker.helper_handler = lambda rec: ExecResult(1, b"", b"no space left")
        self.ui.push(False)  # do not continue without a backup
        before = self.docker.names().count("compose_up")
        with self.assertRaises(UserAbort):
            self.update("0.2.0")
        self.assertEqual(self.docker.names().count("compose_up"), before + 1)
        self.assertEqual(self.docker.up_log[-1][3], "0.1.0")  # the installed release
        self.assertEqual(self.archive_names(), [])  # no half archive stays
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.1.0")
        self.assertIn("no space left", "\n".join(c.text for c in self.ui.calls))

    def test_an_unreadable_archive_counts_as_a_failure(self) -> None:
        self.first()
        self.docker.helper_handler = lambda rec: ExecResult(0)  # exit 0, but no archive written
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.update("0.2.0")
        self.assertEqual(self.archive_names(), [])

    def test_the_user_may_continue_without_the_archive(self) -> None:
        self.first()
        self.docker.helper_handler = lambda rec: ExecResult(1, b"", b"no space left")
        self.ui.push(True)
        result = self.update("0.2.0")
        self.assertEqual(result.volume_backups, ())
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.2.0")

    def test_a_missing_image_is_a_backup_failure_not_a_crash(self) -> None:
        self.first()
        self.docker.images.discard("investment:0.1.0")
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.update("0.2.0")
        self.assertIn("not in Docker", "\n".join(c.text for c in self.ui.calls))

    def test_the_old_archives_go_with_the_old_dumps(self) -> None:
        self.first()
        self.backups.mkdir(exist_ok=True)
        for name in ("uploads-0.0.1-20240101T000000Z.tar.gz",):
            old = self.backups / name
            old.write_bytes(b"x")
            os.utime(old, (1_600_000_000, 1_600_000_000))
        self.ui.push("always")
        self.update("0.2.0")
        names = self.archive_names()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("uploads-0.1.0-"))  # the old one went, the new stays
        info = load_install_info(self.target)
        assert info is not None
        self.assertTrue(info.auto_delete_old_backups)

    def test_an_old_archive_is_offered_once_a_newer_one_exists(self) -> None:
        self.first()
        self.backups.mkdir(exist_ok=True)
        old = self.backups / "uploads-0.0.1-20200101T000000Z.tar.gz"
        old.write_bytes(b"x")
        os.utime(old, (1_500_000_000,) * 2)
        self.ui.push("later")
        self.update("0.2.0")
        self.assertEqual(len(self.archive_names()), 2)  # asked, and the answer was "not now"
        self.assertTrue(any(c.title == "Old backups" for c in self.ui.calls))


class PreflightTests(VolumeCase):
    def test_not_enough_space_for_the_volume_backup(self) -> None:
        self.first()
        self.docker.helper_handler = lambda rec: (
            ExecResult(0, b"5000000\t/data\n") if rec.command == SIZE_COMMAND else None
        )
        self.deps.disk_free = lambda path: 3 * 2**30
        _, release = self.publish("0.2.0", manifest_edits=self.edits)
        job = self.make_job(release)
        problems = self.installer.preflight(
            job.meta, release, self.target, kind=PlanKind.UPDATE, port=2570
        )
        self.assertTrue(any("volume backups" in p for p in problems), problems)

    def test_enough_space(self) -> None:
        self.first()
        _, release = self.publish("0.2.0", manifest_edits=self.edits)
        job = self.make_job(release)
        self.assertEqual(
            self.installer.preflight(
                job.meta, release, self.target, kind=PlanKind.UPDATE, port=2570
            ),
            [],
        )

    def test_a_new_installation_measures_nothing(self) -> None:
        _, release = self.publish("0.1.0", manifest_edits=self.edits)
        job = self.make_job(release)
        self.installer.preflight(job.meta, release, self.target, kind=PlanKind.NEW, port=2570)
        self.assertEqual(self.docker.helper_log, [])


class RecoveryTests(VolumeCase):
    def failing_update(self, *, corrupt: bool) -> Any:
        self.first()
        self.fill(**{"f": b"old data"})
        _, release = self.publish("0.2.0", manifest_edits=self.edits)

        def on_up(project: str, compose: Path, env: Path) -> None:
            version = read_env(env)["APP_VERSION"]
            if version == "0.2.0" and corrupt:
                self.docker.volume_data[SOURCE]["f"] = b"damaged by 0.2.0"
            self.healthy_version = version if version == "0.1.0" else None

        self.docker.on_up = on_up
        return release

    def test_the_menu_offers_a_rollback_with_the_data(self) -> None:
        release = self.failing_update(corrupt=True)
        self.ui.push("keep")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        menu = [c for c in self.ui.calls if c.kind == "menu"][-1]
        self.assertIn("rollback", menu.items)
        self.assertIn("rollback_restore", menu.items)

    def test_rollback_with_restore_brings_the_volume_back(self) -> None:
        release = self.failing_update(corrupt=True)
        self.ui.push("rollback_restore", "investment")  # then the typed application name
        with self.assertRaises(InstallFailed) as ctx:
            self.installer.install(self.make_job(release))
        self.assertIn("was restored and is running", str(ctx.exception))
        self.assertEqual(self.docker.volume_data[SOURCE], {"f": b"old data"})
        names = self.archive_names()
        self.assertTrue(any(n.startswith("uploads-pre-restore-") for n in names), names)
        self.assertTrue(any(n.startswith("uploads-0.1.0-") for n in names), names)

    def test_a_wrong_name_restores_nothing(self) -> None:
        release = self.failing_update(corrupt=True)
        self.ui.push("rollback_restore", "wrong name")
        with self.assertRaises(UserAbort):
            self.installer.install(self.make_job(release))
        self.assertEqual(self.docker.volume_data[SOURCE], {"f": b"damaged by 0.2.0"})

    def test_a_plain_rollback_leaves_the_data_as_it_is(self) -> None:
        release = self.failing_update(corrupt=True)
        self.ui.push("rollback")
        with self.assertRaises(InstallFailed):
            self.installer.install(self.make_job(release))
        self.assertEqual(self.docker.volume_data[SOURCE], {"f": b"damaged by 0.2.0"})

    def test_a_failure_before_the_start_starts_the_old_application_again(self) -> None:
        self.first()
        self.docker.fail = {"load_image"}
        _, release = self.publish("0.2.0", manifest_edits=self.edits)
        self.docker.images.discard("investment:0.2.0")
        starts = self.docker.names().count("compose_up")
        with self.assertRaises(Exception):  # noqa: B017 - DockerError from load_image
            self.installer.install(self.make_job(release))
        self.assertEqual(self.docker.names().count("compose_up"), starts + 1)
        self.assertEqual(self.docker.up_log[-1][3], "0.1.0")


class BindMountTests(VolumeCase):
    def setUp(self) -> None:
        super().setUp()
        self.media = self.target / "shared" / "data" / "media"
        self.mounts[:] = [{"type": "bind", "source": self.media.as_posix(), "target": "/app/media"}]
        self.top.clear()
        self.edits["volumes"] = [
            {"name": "media", "kind": "bind", "hostPath": "media", "mountPath": "/app/media"}
        ]

    def test_the_data_directory_and_the_variables_are_prepared(self) -> None:
        self.first()
        self.assertTrue(self.media.is_dir())
        env = read_env(self.target / "shared" / "compose.env")
        self.assertEqual(env["DATA_DIR"], str(self.target / "shared" / "data"))
        if hasattr(os, "getuid"):
            self.assertEqual(env["APP_UID"], str(os.getuid()))
            self.assertEqual(env["APP_GID"], str(os.getgid()))

    def test_the_directory_survives_the_release_rotation(self) -> None:
        self.first()
        (self.media / "pic.png").write_bytes(b"png")
        for version in ("0.2.0", "0.3.0", "0.4.0"):
            self.update(version)
        self.assertEqual((self.media / "pic.png").read_bytes(), b"png")
        self.assertFalse((self.target / "releases" / "0.1.0").exists())  # rotated away

    def test_a_bind_directory_is_archived_from_its_host_path(self) -> None:
        self.first()
        self.docker.volume_data[self.media.as_posix()] = {"pic.png": b"png"}
        self.update("0.2.0")
        (record,) = self.docker.helper_log
        self.assertEqual(record.mounts[0].source, self.media.as_posix())
        (archive,) = self.backups.glob("media-0.1.0-*.tar.gz")
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.getnames(), ["pic.png"])

    def test_a_bind_mount_elsewhere_is_blocked(self) -> None:
        self.mounts[0]["source"] = "/srv/other"
        with self.assertRaises(ReleaseError):
            self.first()


class ManualBackupAndRestoreTests(VolumeCase):
    def test_backup_now_can_stop_the_application(self) -> None:
        self.first()
        self.fill(a=b"1")
        self.docker.calls.clear()
        dump, archives, problem = self.installer.backup_now(self.target, stop_app=True)
        self.assertIsNone(dump)
        self.assertIsNone(problem)
        self.assertEqual(len(archives), 1)
        order = self.docker.names()
        self.assertLess(order.index("compose_stop"), order.index("run_helper"))
        self.assertLess(order.index("run_helper"), order.index("compose_up"))

    def test_backup_now_online(self) -> None:
        self.first()
        self.docker.calls.clear()
        _, archives, _ = self.installer.backup_now(self.target, stop_app=False)
        self.assertEqual(len(archives), 1)
        self.assertNotIn("compose_stop", self.docker.names())
        self.assertNotIn("compose_up", self.docker.names())

    def test_restore_replaces_the_contents_after_a_safety_copy(self) -> None:
        self.first()
        self.fill(a=b"before")
        _, (archive,), _ = self.installer.backup_now(self.target, stop_app=False)
        self.fill(a=b"after", extra=b"x")
        self.docker.calls.clear()
        self.ui.push("investment")
        problem = self.installer.restore_data(
            self.target, RestoreChoice(volumes=(("uploads", archive),))
        )
        self.assertIsNone(problem)
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"before"})
        pre = [p for p in list_volume_backups(self.backups) if p.version == "pre-restore"]
        self.assertEqual(len(pre), 1)
        with tarfile.open(pre[0].path) as tar:
            self.assertEqual(sorted(tar.getnames()), ["a", "extra"])  # the state before the restore
        order = self.docker.names()
        self.assertLess(order.index("compose_stop"), order.index("compose_up"))

    def test_a_wrong_confirmation_changes_nothing(self) -> None:
        self.first()
        self.fill(a=b"before")
        _, (archive,), _ = self.installer.backup_now(self.target, stop_app=False)
        self.fill(a=b"after")
        self.docker.calls.clear()
        self.docker.helper_log.clear()
        self.ui.push("nope")
        with self.assertRaises(UserAbort):
            self.installer.restore_data(self.target, RestoreChoice(volumes=(("uploads", archive),)))
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"after"})
        self.assertNotIn("compose_stop", self.docker.names())
        self.assertEqual(self.docker.helper_log, [])

    def test_a_failed_safety_copy_stops_the_restore(self) -> None:
        self.first()
        _, (archive,), _ = self.installer.backup_now(self.target, stop_app=False)
        self.fill(a=b"after")
        self.docker.helper_handler = lambda rec: ExecResult(1, b"", b"disk full")
        self.ui.push("investment")
        with self.assertRaises(Exception) as ctx:  # noqa: B017
            self.installer.restore_data(self.target, RestoreChoice(volumes=(("uploads", archive),)))
        self.assertIn("nothing was restored", str(ctx.exception))
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"after"})

    def test_a_corrupt_archive_is_refused_before_anything_is_emptied(self) -> None:
        self.first()
        self.fill(a=b"keep me")
        bad = self.backups / "uploads-0.1.0-20261101T000000Z.tar.gz"
        bad.write_bytes(b"this is not an archive")
        self.ui.push("investment")
        with self.assertRaises(Exception):  # noqa: B017
            self.installer.restore_data(self.target, RestoreChoice(volumes=(("uploads", bad),)))
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"keep me"})
        self.assertFalse(any(RESTORE_COMMAND == r.command for r in self.docker.helper_log))

    def test_a_volume_the_release_does_not_use_is_refused(self) -> None:
        self.first()
        archive = self.backups / "ghost-0.1.0-20261101T000000Z.tar.gz"
        archive.parent.mkdir(exist_ok=True)
        archive.write_bytes(b"x")
        with self.assertRaises(Exception) as ctx:  # noqa: B017
            self.installer.restore_data(self.target, RestoreChoice(volumes=(("ghost", archive),)))
        self.assertIn("ghost", str(ctx.exception))


class NothingIsEverRemovedTests(VolumeCase):
    def test_no_docker_call_can_delete_a_volume(self) -> None:
        for name in ("compose_down", "volume_rm", "volume_remove", "volume_prune", "system_prune"):
            self.assertFalse(hasattr(Docker, name), name)
        ticks = iter(range(1, 1000))
        self.deps.clock = lambda: (
            datetime(2026, 11, 19, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))
        )
        self.first()
        self.fill(a=b"1")
        self.update("0.2.0")
        self.installer.backup_now(self.target, stop_app=True)
        _, (archive,), _ = self.installer.backup_now(self.target, stop_app=False)
        self.ui.push("investment")
        self.installer.restore_data(self.target, RestoreChoice(volumes=(("uploads", archive),)))
        self.update("0.3.0")
        self.update("0.4.0")  # rotates the oldest release away
        allowed = {BACKUP_COMMAND, RESTORE_COMMAND, SIZE_COMMAND}
        for record in self.docker.helper_log:
            self.assertIn(record.command, allowed)
            script = " ".join(record.command)
            if "rm " in script:  # the only deletion is emptying the volume before unpacking
                self.assertEqual(record.command, RESTORE_COMMAND)
                self.assertFalse(record.mounts[0].read_only)
            else:
                self.assertTrue(all(m.read_only for m in record.mounts))
        self.assertTrue(set(self.docker.names()) <= {
            "info", "root_dir", "image_exists", "load_image", "remove_image", "compose_up",
            "compose_stop", "compose_ps", "compose_logs", "compose_config", "run_helper",
            "volume_list", "exec", "list_running_containers", "inspect_container",
        })  # fmt: skip
        self.assertIn(SOURCE, self.docker.volume_data)  # the volume is still there


class PlanTests(VolumeCase):
    def test_the_plan_lists_the_volumes_and_the_backup_step(self) -> None:
        self.first()
        _, release = self.publish("0.2.0", manifest_edits=self.edits)
        plan = self.make_job(release).plan
        self.assertEqual(plan.kind, PlanKind.UPDATE)
        self.assertEqual(plan.volumes, ("uploads (volume) -> /app/uploads, backed up",))
        self.assertIn(StepId.DATA_BACKUP, [s.id for s in plan.steps])
        text = format_plan(plan)
        self.assertIn("Data volumes:", text)
        self.assertIn("uploads (volume)", text)

    def test_a_new_installation_has_nothing_to_back_up(self) -> None:
        _, release = self.publish("0.1.0", manifest_edits=self.edits)
        plan = self.make_job(release).plan
        self.assertNotIn(StepId.DATA_BACKUP, [s.id for s in plan.steps])
        self.assertEqual(len(plan.volumes), 1)

    def test_no_volumes_no_lines(self) -> None:
        _, release = self.publish("0.1.0", manifest_edits={"database": {"required": False}})
        plan = self.make_job(release).plan
        self.assertEqual(plan.volumes, ())
        self.assertNotIn("Data volumes", format_plan(plan))

    def test_a_downgrade_warns_that_the_data_does_not_go_back(self) -> None:
        self.first()
        self.update("0.2.0")
        _, release = self.publish("0.1.0", manifest_edits=self.edits)
        plan = self.make_job(release).plan
        self.assertEqual(plan.kind, PlanKind.DOWNGRADE)
        self.assertTrue(any("do not go back" in w for w in plan.warnings))

    def test_a_volume_without_backup_has_no_backup_step(self) -> None:
        self.edits["volumes"][0]["backup"] = False
        self.first()
        _, release = self.publish("0.2.0", manifest_edits=self.edits)
        plan = self.make_job(release).plan
        self.assertNotIn(StepId.DATA_BACKUP, [s.id for s in plan.steps])
        self.assertIn("NOT backed up", plan.volumes[0])


class ProxyBasePathTests(VolumeCase):
    def test_the_snippet_follows_the_chosen_base_path(self) -> None:
        self.mounts.clear()
        self.top.clear()
        edits = {
            "database": {"required": False},
            "proxy": {"snippet": 'ProxyPass "{basePath}" "http://127.0.0.1:{port}/"'},
        }
        result = self.install("0.1.0", manifest_edits=edits)
        self.assertEqual(result.proxy_snippet, 'ProxyPass "/investment/" "http://127.0.0.1:2570/"')


class StatusTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.docker.config_handler = lambda project, compose, env: {
            "services": {"app": {"volumes": [named_mount()]}},
            "volumes": {"uploads": {"name": SOURCE}},
        }
        self.edits = {"volumes": [{"name": "uploads", "mountPath": "/app/uploads"}]}
        self.ui.push(True)
        self.run_install("0.1.0", manifest_edits=self.edits)

    def test_status_lists_size_and_last_backup(self) -> None:
        self.docker.volume_data[SOURCE] = {"a": b"x" * 4096}
        records = load_installs(self.state_dir / "installs.json")
        (status,) = collect_status(records, self.docker, self.fake_health)
        (row,) = status.volumes
        self.assertEqual(row["name"], "uploads")
        self.assertEqual(row["sizeBytes"], 4096)
        self.assertIsNone(row["lastBackup"])
        text = format_status([status])
        self.assertIn("data volumes:", text)
        self.assertIn("uploads (named)", text)
        self.assertIn("last backup: never", text)

    def test_status_shows_the_newest_archive(self) -> None:
        self.run_install("0.2.0", manifest_edits=self.edits)
        records = load_installs(self.state_dir / "installs.json")
        (status,) = collect_status(records, self.docker, self.fake_health)
        self.assertRegex(str(status.volumes[0]["lastBackup"]), r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(status.to_json()["volumes"][0]["name"], "uploads")  # type: ignore[index]


class BackupMenuTests(AppTestCase):
    with_database = True

    def setUp(self) -> None:
        super().setUp()
        self.docker.config_handler = lambda project, compose, env: {
            "services": {"app": {"volumes": [named_mount()]}},
            "volumes": {"uploads": {"name": SOURCE}},
        }
        self.edits = {"volumes": [{"name": "uploads", "mountPath": "/app/uploads"}]}
        self.ui.push(True)
        self.run_install("0.1.0", manifest_edits=self.edits)
        self.ui.calls.clear()
        self.backups = self.target / "backups"
        self.docker.volume_data[SOURCE] = {"a": b"v1"}

    def test_backup_now_makes_one_set_of_the_database_and_the_volume(self) -> None:
        self.ui.push("backup", True, UiBack)  # stop the application while copying
        self.call(backup_menu)
        (backup_set,) = group_sets(list_all_backups(self.backups))
        self.assertEqual(sorted(f.kind for f in backup_set.files), ["db", "volume"])
        self.assertIn("The backup was created", self.messages())
        self.assertIn(".tar.gz", self.messages())
        self.assertIn(".dump", self.messages())
        confirm = [c for c in self.ui.calls if c.kind == "confirm"][0]
        self.assertIn("Stop the application", confirm.text)

    def test_restore_offers_the_parts_and_restores_what_was_ticked(self) -> None:
        self.ui.push("backup", True, UiBack)
        self.call(backup_menu)
        (backup_set,) = group_sets(list_all_backups(self.backups))
        self.docker.volume_data[SOURCE] = {"a": b"v2"}
        self.docker.calls.clear()
        self.ui.calls.clear()
        # restore only the volume (untick the database), confirm with the application name
        self.ui.push("restore", set_key(backup_set), ["volume:uploads"], "investment", UiBack)
        self.call(backup_menu)
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"v1"})
        self.assertFalse(any(c.startswith("restore:") for c in self.db.calls))
        self.assertIn("restarted", self.messages())
        parts = [c for c in self.ui.calls if c.kind == "choose_many"][0]
        self.assertEqual(sorted(parts.items), ["db:investment", "volume:uploads"])

    def test_restore_of_both_parts(self) -> None:
        self.ui.push("backup", True, UiBack)
        self.call(backup_menu)
        (backup_set,) = group_sets(list_all_backups(self.backups))
        self.docker.volume_data[SOURCE] = {"a": b"v2"}
        self.ui.push(
            "restore",
            set_key(backup_set),
            ["db:investment", "volume:uploads"],
            "investment",
            UiBack,
        )
        self.call(backup_menu)
        self.assertTrue(any(c.startswith("restore:") for c in self.db.calls))
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"v1"})

    def test_ticking_nothing_restores_nothing(self) -> None:
        self.ui.push("backup", True, UiBack)
        self.call(backup_menu)
        (backup_set,) = group_sets(list_all_backups(self.backups))
        self.docker.volume_data[SOURCE] = {"a": b"v2"}
        self.ui.push("restore", set_key(backup_set), [], UiBack)
        self.call(backup_menu)
        self.assertEqual(self.docker.volume_data[SOURCE], {"a": b"v2"})
        self.assertIn("Nothing was selected", self.messages())

    def test_cleanup_keeps_the_newest_of_each_series(self) -> None:
        self.backups.mkdir(exist_ok=True)
        for name in (
            "investment-0.0.1-20240101T000000Z.dump",
            "uploads-0.0.1-20240101T000000Z.tar.gz",
        ):
            path = self.backups / name
            path.write_bytes(b"x")
            os.utime(path, (1_600_000_000, 1_600_000_000))
        self.ui.push("cleanup", UiBack)
        self.call(backup_menu)
        # the only file of each series is protected: nothing is old enough to offer
        self.assertTrue(all(p.exists() for p in self.backups.iterdir()))
        self.assertEqual(len(list(self.backups.iterdir())), 2)


class StatusWithoutVolumesTests(AppTestCase):
    def test_no_volumes_no_section_and_no_helper_container(self) -> None:
        self.run_install("0.1.0")
        records = load_installs(self.state_dir / "installs.json")
        (status,) = collect_status(records, self.docker, self.fake_health)
        self.assertEqual(status.volumes, ())
        self.assertNotIn("data volumes", format_status([status]))
        self.assertEqual(self.docker.helper_log, [])


class VolumeOnlyApplicationTests(AppTestCase):
    """No database at all: the Backups menu is still there for the volumes."""

    def test_the_menu_is_available_with_volumes_only(self) -> None:
        self.docker.config_handler = lambda project, compose, env: {
            "services": {"app": {"volumes": [named_mount()]}},
            "volumes": {"uploads": {"name": SOURCE}},
        }
        self.ui.push(True)
        self.run_install(
            "0.1.0", manifest_edits={"volumes": [{"name": "uploads", "mountPath": "/app/uploads"}]}
        )
        self.ui.calls.clear()
        self.ui.push("backup", True, UiBack)
        self.call(backup_menu)
        self.assertEqual(len(list((self.target / "backups").glob("uploads-0.1.0-*.tar.gz"))), 1)

    def test_an_application_without_data_is_not_offered(self) -> None:
        self.run_install("0.1.0")
        self.call(backup_menu)
        self.assertIn("no suitable", self.messages())


if __name__ == "__main__":
    unittest.main()
