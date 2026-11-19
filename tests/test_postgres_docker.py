"""Real PostgreSQL tests against throw-away containers (specification 11, "Valódi Docker tesztek").

For each of ``postgres:17-alpine`` and ``postgres:15-alpine`` a private container on a private
network is started and removed again; containers the machine already runs are never touched.
Skipped when Docker is unavailable or ``TELEPITO_SKIP_DOCKER_TESTS`` is set. The first run may
need to pull the images.
"""

import base64
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from telepito.docker_cli import SubprocessDocker
from telepito.logging_ import Logger
from telepito.manifest import parse_manifest
from telepito.postgres import (
    PostgresError,
    PostgresOps,
    PsqlClient,
    find_postgres_containers,
    scram_sha256_verifier,
)
from telepito.ui.scripted import ScriptedUi

from .test_wizard import MemoryVault

D = ScriptedUi.DEFAULT
IMAGES = ["postgres:17-alpine", "postgres:15-alpine"]


def _run(*args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_usable() -> bool:
    if os.environ.get("TELEPITO_SKIP_DOCKER_TESTS") or shutil.which("docker") is None:
        return False
    try:
        return _run("info", "--format", "{{.ServerVersion}}", timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


MANIFEST = parse_manifest(
    {
        "schemaVersion": 1,
        "name": "investment",
        "image": {"name": "investment"},
        "compose": {},
        "port": {"default": 2570},
        "health": {"path": "/healthz"},
        "database": {
            "required": True,
            "description": "Adatbázis kell.",
            "urlEnv": "DATABASE_URL",
            "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
        },
    }
)


class ServerFixture:
    """One throw-away Postgres container on its own network."""

    def __init__(self, image: str) -> None:
        suffix = secrets.token_hex(4)
        self.image = image
        self.name = f"tp-test-{image.split(':')[1].split('-')[0]}-{suffix}"
        self.network = f"tp-test-net-{suffix}"

    def start(self) -> None:
        if _run("image", "inspect", self.image).returncode != 0:
            pulled = _run("pull", self.image, timeout=900)
            if pulled.returncode != 0:
                raise unittest.SkipTest(f"{self.image} nem tölthető le")
        _run("network", "create", self.network)
        started = _run(
            "run", "-d", "--name", self.name, "--network", self.network,
            "--network-alias", "postgres", "-e", "POSTGRES_PASSWORD=unused-admin-pw",
            self.image,
        )  # fmt: skip
        if started.returncode != 0:
            self.stop()
            raise RuntimeError(started.stderr)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            # The image's init phase runs a temporary server first; wait for the real one.
            logs = _run("logs", self.name)
            ready = (logs.stdout + logs.stderr).count("ready to accept connections")
            if (
                ready >= 2
                and _run("exec", self.name, "pg_isready", "-U", "postgres").returncode == 0
            ):
                return
            time.sleep(1)
        self.stop()
        raise RuntimeError("A Postgres nem indult el időben.")

    def stop(self) -> None:
        _run("rm", "-f", "-v", self.name)
        _run("network", "rm", self.network)


@unittest.skipUnless(docker_usable(), "docker is not available")
class PostgresDockerBase(unittest.TestCase):
    image = IMAGES[0]
    fixture: ServerFixture

    @classmethod
    def setUpClass(cls) -> None:
        if cls is PostgresDockerBase:
            raise unittest.SkipTest("abstract")
        cls.fixture = ServerFixture(cls.image)
        cls.fixture.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls is not PostgresDockerBase:
            cls.fixture.stop()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.docker = SubprocessDocker()
        self.log = Logger(None)
        self.vault = MemoryVault()
        self.ui = ScriptedUi()
        self.ops = PostgresOps(self.docker, self.ui, self.log, vault_provider=lambda: self.vault)
        self.client = PsqlClient(self.docker, self.fixture.name, "postgres")
        self.suffix = secrets.token_hex(3)
        self.db = f"app_{self.suffix}"
        self.user = f"user_{self.suffix}"

    def pick_server(self) -> list[Any]:
        """The answers for the server questions (depends on how many Postgres run here)."""
        count = len(find_postgres_containers(self.docker))
        return ([True] if count == 1 else [self.fixture.name]) + [D, D]

    def create(self, extra: list[Any]) -> Any:
        self.ui.push(*self.pick_server(), self.db, self.user, *extra)
        return self.ops.create(MANIFEST)

    def password_of(self, url: str) -> str:
        return url.split(":")[2].split("@")[0]


class _Tests:
    """The actual tests; mixed into one class per Postgres version below."""

    def test_create_login_and_scram_verifier_matches_postgres(self: Any) -> None:
        setup = self.create(["generate"])
        info = setup.info
        self.assertEqual((info.name, info.user, info.host), (self.db, self.user, "postgres"))
        self.assertEqual(info.container, self.fixture.name)

        stored = self.client.run(
            f"SELECT rolpassword FROM pg_authid WHERE rolname = '{self.user}';"
        )
        self.assertTrue(stored.startswith("SCRAM-SHA-256$4096:"), stored)
        # Recompute the verifier with the *server's* salt: it must be identical.
        salt_b64 = stored.split("$")[1].split(":")[1]
        password = self.password_of(setup.env["DATABASE_URL"])
        recomputed = scram_sha256_verifier(password, salt=base64.b64decode(salt_b64))
        self.assertEqual(recomputed, stored)

        # Logging in over the network address enforces SCRAM (create() already did it once).
        self.client.check_login("postgres", self.db, self.user, password)
        with self.assertRaises(PostgresError):
            self.client.check_login("postgres", self.db, self.user, password + "x")

        owner = self.client.run(
            f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{self.db}';"
        )
        self.assertEqual(owner, self.user)
        self.assertEqual(self.vault.get("investment", "DB_PASSWORD"), password)

    def test_special_characters_in_a_manual_password(self: Any) -> None:
        tricky = "A!b#c$d%e&f'g\"h:i/j@k"
        setup = self.create(["manual", tricky, tricky])
        self.client.check_login("postgres", self.db, self.user, tricky)
        url = setup.env["DATABASE_URL"]
        self.assertNotIn("#", url.split("@")[0])  # percent-encoded
        self.assertTrue(url.endswith(f"@postgres:5432/{self.db}"))

    def test_existing_objects_can_be_reused_with_the_right_password(self: Any) -> None:
        first = self.create(["generate"])
        password = self.password_of(first.env["DATABASE_URL"])
        self.ui.push(*self.pick_server(), self.db, self.user, "use", password)
        second = self.ops.create(MANIFEST)
        self.assertEqual(second.info.name, self.db)

    def test_reusing_with_a_wrong_password_fails(self: Any) -> None:
        self.create(["generate"])
        self.ui.push(*self.pick_server(), self.db, self.user, "use", "wrong-password-1")
        with self.assertRaises(PostgresError):
            self.ops.create(MANIFEST)

    def test_backup_and_restore_round_trip(self: Any) -> None:
        info = self.create(["generate"]).info
        self.client.run(
            "CREATE TABLE notes (id int PRIMARY KEY, body text);"
            "INSERT INTO notes VALUES (1, 'one'), (2, 'two');",
            database=self.db,
        )
        backups = self.dir / "backups"
        dump = self.ops.backup(info, "0.1.0", backups)
        assert dump is not None
        self.assertGreater(dump.stat().st_size, 0)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(dump.stat().st_mode), 0o600)
        self.assertRegex(dump.name, rf"^{self.db}-0\.1\.0-\d{{8}}T\d{{6}}Z\.dump$")

        self.client.run("DELETE FROM notes WHERE id = 1; INSERT INTO notes VALUES (3, 'three');",
                        database=self.db)  # fmt: skip
        self.ui.push(self.db)  # type the database name to confirm
        self.ops.restore(info, dump)

        rows = self.client.run("SELECT id || ':' || body FROM notes ORDER BY id;", database=self.db)
        self.assertEqual(rows.split("\n"), ["1:one", "2:two"])
        owner = self.client.run(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'notes';", database=self.db
        )
        self.assertEqual(owner, self.user)
        # the state before the restore was kept as a safety copy
        safety = [p for p in backups.iterdir() if "pre-restore" in p.name]
        self.assertEqual(len(safety), 1)
        # and the application's own role can still use the restored database
        password = self.vault.get("investment", "DB_PASSWORD")
        assert password is not None
        self.client.check_login("postgres", self.db, self.user, password)

    def test_password_rotation(self: Any) -> None:
        setup = self.create(["generate"])
        info = setup.info
        old = self.password_of(setup.env["DATABASE_URL"])
        env_path = self.dir / ".env.production"
        env_path.write_text(f"DATABASE_URL='{setup.env['DATABASE_URL']}'\n", encoding="utf-8")
        self.ui.push("generate")
        self.ops.rotate_password(MANIFEST, info, env_path)
        new_url = env_path.read_text(encoding="utf-8").split("'")[1]
        new = self.password_of(new_url)
        self.assertNotEqual(old, new)
        self.client.check_login("postgres", self.db, self.user, new)
        with self.assertRaises(PostgresError):
            self.client.check_login("postgres", self.db, self.user, old)

    def test_a_missing_database_makes_the_backup_fail_cleanly(self: Any) -> None:
        info = self.create(["generate"]).info
        self.client.run(f'DROP DATABASE "{self.db}";')
        self.ui.push(False)  # do not continue without a backup
        from telepito.errors import UserAbort

        with self.assertRaises(UserAbort):
            self.ops.backup(info, "0.1.0", self.dir / "backups")
        self.assertEqual(list((self.dir / "backups").iterdir()), [])

    def test_wrong_confirmation_does_not_touch_the_database(self: Any) -> None:
        from telepito.errors import UserAbort

        info = self.create(["generate"]).info
        self.client.run("CREATE TABLE keep (x int); INSERT INTO keep VALUES (1);", database=self.db)
        dump = self.dir / "x.dump"
        dump.write_bytes(b"irrelevant")
        self.ui.push("not-the-name")
        with self.assertRaises(UserAbort):
            self.ops.restore(info, dump)
        self.assertEqual(self.client.run("SELECT count(*) FROM keep;", database=self.db), "1")


class Postgres17Tests(_Tests, PostgresDockerBase):
    image = IMAGES[0]


class Postgres15Tests(_Tests, PostgresDockerBase):
    image = IMAGES[1]


if __name__ == "__main__":
    unittest.main()
