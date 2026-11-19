import base64
import hashlib
import hmac
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from installer.docker import ContainerInfo, DockerError, ExecResult
from installer.envfile import read_env
from installer.errors import UserAbort
from installer.logging_ import Logger
from installer.manifest import Manifest, parse_manifest
from installer.postgres import (
    PostgresError,
    PostgresOps,
    PsqlClient,
    build_url,
    choose_server,
    default_host,
    find_postgres_containers,
    password_clause,
    quote_ident,
    quote_literal,
    scram_keys,
    scram_sha256_verifier,
    validate_password,
)
from installer.state import DatabaseInfo
from installer.ui.base import UiBack
from installer.ui.scripted import ScriptedUi

from .fake_docker import ExecRecord, FakeDocker
from .test_wizard import MemoryVault

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
D = ScriptedUi.DEFAULT  # "press Enter": accept the prefilled default


def pg_container(
    name: str = "database-postgres-1",
    image: str = "postgres:17-alpine",
    networks: dict[str, tuple[str, ...]] | None = None,
    env: dict[str, str] | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        image=image,
        env=env if env is not None else {"POSTGRES_USER": "postgres"},
        exposed_ports=("5432/tcp",),
        networks=networks
        if networks is not None
        else {"shared-infra": ("database-postgres-1", "abcdef012345", "postgres")},
    )


def manifest() -> Manifest:
    return parse_manifest(
        {
            "schemaVersion": 1,
            "name": "investment",
            "image": {"name": "investment"},
            "compose": {},
            "port": {"default": 2570},
            "health": {"path": "/healthz"},
            "database": {
                "required": True,
                "description": "Az alkalmazás Postgresben tárolja az adatokat.",
                "urlEnv": "DATABASE_URL",
                "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
                "defaultDatabase": "investment",
                "defaultUser": "investment",
            },
        }
    )


class ScramTests(unittest.TestCase):
    def test_rfc7677_vector(self) -> None:
        """RFC 7677 section 3: user "user", password "pencil", SCRAM-SHA-256."""
        salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
        keys = scram_keys("pencil", salt, 4096)
        auth_message = (
            b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
            b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
        )
        signature = hmac.new(keys.stored_key, auth_message, hashlib.sha256).digest()
        proof = bytes(a ^ b for a, b in zip(keys.client_key, signature, strict=True))
        self.assertEqual(
            base64.b64encode(proof).decode(), "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
        )
        server_signature = hmac.new(keys.server_key, auth_message, hashlib.sha256).digest()
        self.assertEqual(
            base64.b64encode(server_signature).decode(),
            "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=",
        )

    def test_verifier_format(self) -> None:
        salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
        verifier = scram_sha256_verifier("pencil", salt=salt)
        prefix, _, rest = verifier.partition("$")
        iterations_salt, _, keys = rest.partition("$")
        self.assertEqual(prefix, "SCRAM-SHA-256")
        self.assertEqual(iterations_salt, "4096:W22ZaJ0SNY7soEsUEjb6gQ==")
        stored, _, server = keys.partition(":")
        expected = scram_keys("pencil", salt)
        self.assertEqual(base64.b64decode(stored), expected.stored_key)
        self.assertEqual(base64.b64decode(server), expected.server_key)

    def test_random_salt_and_no_plaintext(self) -> None:
        first, second = scram_sha256_verifier("secretvalue1"), scram_sha256_verifier("secretvalue1")
        self.assertNotEqual(first, second)
        self.assertNotIn("secretvalue1", first)

    def test_password_clause(self) -> None:
        self.assertTrue(password_clause("pw123456", "scram-sha-256").startswith("'SCRAM-SHA-256$"))
        self.assertEqual(password_clause("pw123456", "md5"), "'pw123456'")


class QuotingTests(unittest.TestCase):
    def test_identifier(self) -> None:
        self.assertEqual(quote_ident("investment"), '"investment"')
        self.assertEqual(quote_ident('we"ird'), '"we""ird"')
        for bad in ["", "a\x00b"]:
            with self.assertRaises(PostgresError):
                quote_ident(bad)

    def test_literal(self) -> None:
        self.assertEqual(quote_literal("it's"), "'it''s'")
        self.assertEqual(quote_literal("a\\b"), "'a\\b'")  # needs standard_conforming_strings
        with self.assertRaises(PostgresError):
            quote_literal("a\x00b")

    def test_injection_attempts_stay_inside_the_literal(self) -> None:
        evil = "x'; DROP DATABASE postgres; --"
        self.assertEqual(quote_literal(evil), "'x''; DROP DATABASE postgres; --'")

    def test_password_validation(self) -> None:
        self.assertIsNone(validate_password("Abcdef12!@#"))
        for bad in ["short", "has space 123", "ékezetes-jelszó-1", "x" * 200, "tab\tbed12345"]:
            with self.subTest(password=bad):
                self.assertIsNotNone(validate_password(bad))

    def test_url_building_is_single_pass_and_encoded(self) -> None:
        url = build_url(
            "postgresql://{user}:{password}@{host}:{port}/{database}",
            user="app",
            password="p{host}:@/?#%",
            host="postgres",
            port=5432,
            database="investment",
        )
        self.assertEqual(
            url, "postgresql://app:p%7Bhost%7D%3A%40%2F%3F%23%25@postgres:5432/investment"
        )


class DiscoveryTests(unittest.TestCase):
    def test_detection_by_image_and_by_port_and_env(self) -> None:
        docker = FakeDocker()
        docker.containers = {
            "a": pg_container("a"),
            "b": pg_container("b", image="bitnami/postgresql:16"),
            "c": ContainerInfo("c", "mycorp/db:1", {"POSTGRES_PASSWORD": "x"}, ("5432/tcp",)),
            "d": ContainerInfo("d", "nginx:1", {}, ("80/tcp",)),
            "e": ContainerInfo("e", "redis:7", {"X": "1"}, ("6379/tcp",)),
        }
        self.assertEqual([c.name for c in find_postgres_containers(docker)], ["a", "b", "c"])

    def test_admin_user_defaults(self) -> None:
        docker = FakeDocker()
        docker.containers = {
            "a": pg_container("a", env={"POSTGRES_USER": "games_center"}),
            "b": pg_container("b", env={}),
        }
        self.assertEqual(
            [c.admin_user for c in find_postgres_containers(docker)], ["games_center", "postgres"]
        )

    def test_default_host_prefers_the_service_alias(self) -> None:
        docker = FakeDocker()
        docker.containers = {"a": pg_container()}
        container = find_postgres_containers(docker)[0]
        self.assertEqual(default_host(container, "shared-infra"), "postgres")
        bare = find_postgres_containers(self._with({"n": ("db-1", "0123456789ab")}, "db-1"))[0]
        self.assertEqual(default_host(bare, "n"), "db-1")

    @staticmethod
    def _with(networks: dict[str, tuple[str, ...]], name: str) -> FakeDocker:
        docker = FakeDocker()
        docker.containers = {name: pg_container(name, networks=networks)}
        return docker


class ChooseServerTests(unittest.TestCase):
    def containers(self, *items: ContainerInfo):  # type: ignore[no-untyped-def]
        docker = FakeDocker()
        docker.containers = {c.name: c for c in items}
        return find_postgres_containers(docker)

    def test_none_found_explains_why_a_database_is_needed(self) -> None:
        with self.assertRaises(PostgresError) as ctx:
            choose_server(ScriptedUi(), [], "Postgres kell az adatokhoz.")
        self.assertIn("Nem található futó Postgres-konténer", str(ctx.exception))
        self.assertIn("Postgres kell az adatokhoz.", str(ctx.exception))

    def test_single_container_needs_confirmation(self) -> None:
        ui = ScriptedUi([True, D, D])  # confirm, host default, admin default
        choice = choose_server(ui, self.containers(pg_container()), "")
        self.assertEqual(
            (choice.container.name, choice.network, choice.host, choice.admin_user),
            ("database-postgres-1", "shared-infra", "postgres", "postgres"),
        )

    def test_declining_the_single_container_aborts(self) -> None:
        with self.assertRaises(UserAbort):
            choose_server(ScriptedUi([False]), self.containers(pg_container()), D)

    def test_several_containers_default_to_the_shared_infra_one(self) -> None:
        other = pg_container(
            "kozitabor-db", "postgres:15-alpine", {"kozitabor_default": ("kozitabor-db", "db")}
        )
        shared = pg_container()
        ui = ScriptedUi(["kozitabor-db", "db-custom", "app_admin"])
        choice = choose_server(ui, self.containers(other, shared), "")
        self.assertEqual(choice.container.name, "kozitabor-db")
        self.assertEqual(
            (choice.network, choice.host, choice.admin_user),
            ("kozitabor_default", "db-custom", "app_admin"),
        )

    def test_default_in_the_radiolist_is_the_shared_infra_container(self) -> None:
        # The ScriptedUi ignores defaults, so check what the real Ui would be told.
        recorded: dict[str, str | None] = {}

        class Spy(ScriptedUi):
            def choose(self, title, items, *, text="", default=None):  # type: ignore[no-untyped-def]
                recorded[title] = default
                return super().choose(title, items, text=text, default=default)

        other = pg_container(
            "kozitabor-db", "postgres:15-alpine", {"kozitabor_default": ("kozitabor-db",)}
        )
        ui = Spy(["database-postgres-1", D, D])
        choose_server(ui, self.containers(other, pg_container()), "")
        self.assertEqual(recorded["Adatbázis-kiszolgáló"], "database-postgres-1")

    def test_network_choice_when_on_several(self) -> None:
        multi = pg_container(networks={"shared-infra": ("postgres",), "pub_default": ("pgdb",)})
        ui = ScriptedUi([True, "pub_default", D, D])
        choice = choose_server(ui, self.containers(multi), "")
        self.assertEqual((choice.network, choice.host), ("pub_default", "pgdb"))

    def test_invalid_host_and_admin_are_asked_again(self) -> None:
        ui = ScriptedUi([True, "bad host!", "pg", "bad-admin;", "dbadmin"])
        choice = choose_server(ui, self.containers(pg_container()), "")
        self.assertEqual((choice.host, choice.admin_user), ("pg", "dbadmin"))
        self.assertEqual(len(ui.messages()), 2)


class PsqlClientTests(unittest.TestCase):
    def test_sql_goes_to_stdin_never_to_arguments(self) -> None:
        docker = FakeDocker()
        client = PsqlClient(docker, "db", "postgres")
        client.run("SELECT 'topsecret';")
        (record,) = docker.exec_log
        self.assertIn("topsecret", record.sql)
        self.assertNotIn("topsecret", " ".join(record.args))
        self.assertIn("standard_conforming_strings", record.sql)
        self.assertEqual(record.args[:3], ("psql", "-U", "postgres"))
        self.assertIn("ON_ERROR_STOP=1", record.args)

    def test_errors_become_postgres_errors(self) -> None:
        docker = FakeDocker()
        docker.exec_results = [ExecResult(1, b"", b"ERROR:  role already exists\n")]
        with self.assertRaises(PostgresError) as ctx:
            PsqlClient(docker, "db", "postgres").run("CREATE ROLE x;")
        self.assertIn("role already exists", str(ctx.exception))

    def test_docker_errors_are_wrapped(self) -> None:
        docker = FakeDocker()
        docker.fail.add("exec")
        with self.assertRaises(PostgresError):
            PsqlClient(docker, "db", "postgres").run("SELECT 1;")

    def test_login_check_uses_a_private_pgpass_file_via_stdin(self) -> None:
        docker = FakeDocker()
        PsqlClient(docker, "db", "postgres").check_login("pg", "investment", "app", "pa:ss\\word1")
        write, login, cleanup = docker.exec_log
        self.assertEqual(write.args[:2], ("sh", "-c"))
        self.assertIn("umask 077", write.args[2])
        self.assertEqual(write.sql, "pg:5432:investment:app:pa\\:ss\\\\word1\n")
        for record in (write, login, cleanup):
            self.assertNotIn("word1", " ".join(record.args))
        self.assertIn("-w", login.args)  # never prompt
        self.assertTrue(login.args[1].startswith("PGPASSFILE=/tmp/.telepito-pgpass-"))
        self.assertEqual(cleanup.args[:2], ("rm", "-f"))

    def test_failed_login_names_the_target_and_still_cleans_up(self) -> None:
        docker = FakeDocker()
        docker.exec_results = [ExecResult(0), ExecResult(2, b"", b"password authentication failed")]
        with self.assertRaises(PostgresError) as ctx:
            PsqlClient(docker, "db", "postgres").check_login("pg", "investment", "app", "pw123456")
        self.assertIn("app@pg/investment", str(ctx.exception))
        self.assertEqual(docker.exec_log[-1].args[:2], ("rm", "-f"))


class OpsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.docker = FakeDocker()
        self.docker.containers = {"database-postgres-1": pg_container()}
        self.ui = ScriptedUi()
        self.log = Logger(None)
        self.vault = MemoryVault()
        self.existing_dbs: set[str] = set()
        self.existing_roles: set[str] = set()
        self.encryption = "scram-sha-256"
        self.docker.exec_handler = self.handle
        self.ops = PostgresOps(
            self.docker, self.ui, self.log, vault_provider=lambda: self.vault, clock=lambda: NOW
        )

    def handle(self, record: ExecRecord) -> ExecResult | None:
        sql = record.sql
        if "FROM pg_database" in sql:
            name = sql.split("datname = '")[1].split("'")[0]
            return ExecResult(0, b"1\n" if name in self.existing_dbs else b"")
        if "FROM pg_roles" in sql:
            name = sql.split("rolname = '")[1].split("'")[0]
            return ExecResult(0, b"1\n" if name in self.existing_roles else b"")
        if "SHOW password_encryption" in sql:
            return ExecResult(0, self.encryption.encode() + b"\n")
        return None

    def statements(self) -> list[str]:
        return [r.sql for r in self.docker.exec_log if r.args and r.args[0] == "psql"]


class CreateTests(OpsTestCase):
    def create(self, answers: list[object]) -> object:
        self.ui.push(*answers)
        return self.ops.create(manifest())

    def test_new_database_end_to_end(self) -> None:
        # confirm container, host, admin, db name, user, "generate"
        setup = self.create([True, D, D, D, D, "generate"])
        info = setup.info  # type: ignore[attr-defined]
        self.assertEqual(
            (info.container, info.network, info.host, info.name, info.user, info.admin_user),
            (
                "database-postgres-1",
                "shared-infra",
                "postgres",
                "investment",
                "investment",
                "postgres",
            ),
        )
        sql = "\n".join(self.statements())
        self.assertIn('CREATE ROLE "investment" LOGIN PASSWORD \'SCRAM-SHA-256$4096:', sql)
        self.assertIn('CREATE DATABASE "investment" OWNER "investment";', sql)
        self.assertIn('GRANT ALL ON SCHEMA public TO "investment";', sql)

        url = setup.env["DATABASE_URL"]  # type: ignore[attr-defined]
        password = url.split(":")[2].split("@")[0]
        self.assertTrue(url.endswith("@postgres:5432/investment"))
        # the plain password never reached psql input or any argument
        self.assertNotIn(password, sql)
        for record in self.docker.exec_log:
            self.assertNotIn(password, " ".join(record.args))
        # it was masked from the log, saved to the vault, and a login test ran
        self.assertEqual(self.vault.get("investment", "DB_PASSWORD"), password)
        self.assertEqual(self.log.redactor.redact(url), "***")
        self.assertTrue(any("-w" in r.args for r in self.docker.exec_log))

    def test_description_is_shown_first(self) -> None:
        self.create([True, D, D, D, D, "generate"])
        self.assertIn("Az alkalmazás Postgresben", self.ui.messages()[0])

    def test_manual_password(self) -> None:
        setup = self.create([True, D, D, D, D, "manual", "Hunter2-Hunter2", "Hunter2-Hunter2"])
        self.assertIn("Hunter2-Hunter2", setup.env["DATABASE_URL"])  # type: ignore[attr-defined]

    def test_invalid_identifier_is_asked_again(self) -> None:
        self.create([True, D, D, "Bad-Name", "good_db", D, "generate"])
        self.assertEqual(len(self.ui.messages()), 2)  # description + validation complaint

    def test_plaintext_fallback_warns(self) -> None:
        self.encryption = "md5"
        self.create([True, D, D, D, D, "generate"])
        self.assertTrue(any("md5" in m for m in self.ui.messages()))
        self.assertNotIn("SCRAM-SHA-256$", "\n".join(self.statements()))

    def test_existing_names_offer_rename(self) -> None:
        self.existing_dbs = {"investment"}
        setup = self.create([True, D, D, D, D, "rename", "investment2", D, "generate"])
        self.assertEqual(setup.info.name, "investment2")  # type: ignore[attr-defined]

    def test_use_existing_asks_the_password_and_creates_nothing(self) -> None:
        self.existing_dbs, self.existing_roles = {"investment"}, {"investment"}
        setup = self.create([True, D, D, D, D, "use", "Existing-Pass-1"])
        sql = "\n".join(self.statements())
        self.assertNotIn("CREATE ROLE", sql)
        self.assertNotIn("CREATE DATABASE", sql)
        self.assertIn("Existing-Pass-1", setup.env["DATABASE_URL"])  # type: ignore[attr-defined]
        self.assertTrue(any("-w" in r.args for r in self.docker.exec_log))  # login verified

    def test_existing_role_only_creates_the_database(self) -> None:
        self.existing_roles = {"investment"}
        self.create([True, D, D, D, D, "use", "Existing-Pass-1"])
        sql = "\n".join(self.statements())
        self.assertNotIn("CREATE ROLE", sql)
        self.assertIn("CREATE DATABASE", sql)

    def test_failed_database_creation_removes_the_role_it_made(self) -> None:
        def fail_db(record: ExecRecord) -> ExecResult | None:
            if "CREATE DATABASE" in record.sql:
                return ExecResult(1, b"", b"ERROR: could not create")
            return self.handle(record)

        self.docker.exec_handler = fail_db
        with self.assertRaises(PostgresError):
            self.create([True, D, D, D, D, "generate"])
        self.assertIn('DROP ROLE IF EXISTS "investment"', "\n".join(self.statements()))

    def test_failed_login_check_is_a_hard_error(self) -> None:
        def fail_login(record: ExecRecord) -> ExecResult | None:
            if "-w" in record.args:
                return ExecResult(2, b"", b"password authentication failed")
            return self.handle(record)

        self.docker.exec_handler = fail_login
        with self.assertRaises(PostgresError) as ctx:
            self.create([True, D, D, D, D, "generate"])
        self.assertIn("bejelentkezési próba", str(ctx.exception))

    def test_no_postgres_container(self) -> None:
        self.docker.containers = {}
        self.assertEqual(len(self.ops.precheck(manifest())), 1)
        self.assertIn("Postgres", self.ops.precheck(manifest())[0])
        with self.assertRaises(PostgresError):
            self.create([])

    def test_precheck_is_clean_when_a_container_exists(self) -> None:
        self.assertEqual(self.ops.precheck(manifest()), [])

    def test_escape_aborts_before_anything_is_created(self) -> None:
        with self.assertRaises(UiBack):
            self.create([True, D, D, D, D, UiBack])
        self.assertNotIn("CREATE ROLE", "\n".join(self.statements()))

    def test_vault_trouble_does_not_fail_the_installation(self) -> None:
        self.ops = PostgresOps(
            self.docker, self.ui, self.log, vault_provider=lambda: None, clock=lambda: NOW
        )
        self.create([True, D, D, D, D, "generate"])
        self.assertTrue(any("jelszótárba" in m for m in self.ui.messages()))


class BackupTests(OpsTestCase):
    INFO = DatabaseInfo(
        "database-postgres-1", "shared-infra", "postgres", "investment", "investment"
    )

    def dump_ok(self, record: ExecRecord) -> ExecResult | None:
        if record.args[:1] == ("pg_dump",):
            return ExecResult(0, b"PGDMP-data")
        return ExecResult(0)

    def test_successful_backup(self) -> None:
        self.docker.exec_handler = self.dump_ok
        path = self.ops.backup(self.INFO, "0.1.0", self.dir / "backups")
        assert path is not None
        self.assertEqual(path.name, "investment-0.1.0-20261119T120000Z.dump")
        self.assertEqual(path.read_bytes(), b"PGDMP-data")
        dump, check = self.docker.exec_log
        self.assertEqual(dump.args, ("pg_dump", "-U", "postgres", "-Fc", "investment"))
        self.assertEqual(check.args, ("pg_restore", "--list"))
        self.assertEqual(check.stdin_from, path)

    def test_failed_dump_leaves_no_file_and_asks(self) -> None:
        self.docker.exec_results = [ExecResult(1, b"", b"pg_dump: error")]
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.ops.backup(self.INFO, "0.1.0", self.dir / "backups")
        self.assertEqual(list((self.dir / "backups").iterdir()), [])

    def test_user_may_insist_on_continuing_without_a_backup(self) -> None:
        self.docker.exec_results = [ExecResult(1, b"", b"pg_dump: error")]
        self.ui.push(True)
        self.assertIsNone(self.ops.backup(self.INFO, "0.1.0", self.dir / "backups"))
        confirm = next(c for c in self.ui.calls if c.kind == "confirm")
        self.assertIn("mentés nélkül", confirm.text)

    def test_empty_or_unreadable_dump_counts_as_failure(self) -> None:
        self.docker.exec_results = [ExecResult(0, b"")]  # empty output
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.ops.backup(self.INFO, "0.1.0", self.dir / "backups")

        def unreadable(record: ExecRecord) -> ExecResult | None:
            if record.args[0] == "pg_dump":
                return ExecResult(0, b"data")
            return ExecResult(1, b"", b"bad archive")

        self.docker.exec_handler = unreadable
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.ops.backup(self.INFO, "0.1.0", self.dir / "backups")
        self.assertEqual(list((self.dir / "backups").iterdir()), [])

    def test_docker_failure_is_handled_like_any_failure(self) -> None:
        self.docker.fail.add("exec")
        self.ui.push(False)
        with self.assertRaises(UserAbort):
            self.ops.backup(self.INFO, "0.1.0", self.dir / "backups")


class RestoreTests(OpsTestCase):
    INFO = BackupTests.INFO

    def setUp(self) -> None:
        super().setUp()
        self.backups = self.dir / "backups"
        self.backups.mkdir()
        self.dump = self.backups / "investment-0.1.0-20261101T000000Z.dump"
        self.dump.write_bytes(b"old-dump")
        self.docker.exec_handler = lambda r: (
            ExecResult(0, b"data") if r.args[0] == "pg_dump" else None
        )

    def test_wrong_confirmation_changes_nothing(self) -> None:
        self.ui.push("wrong")
        with self.assertRaises(UserAbort):
            self.ops.restore(self.INFO, self.dump)
        self.assertEqual(self.docker.exec_log, [])

    def test_restore_sequence_and_safety_copy(self) -> None:
        self.ui.push("investment")
        self.ops.restore(self.INFO, self.dump)
        steps = [r.args[0] for r in self.docker.exec_log]
        self.assertEqual(steps, ["pg_dump", "pg_restore", "psql", "pg_restore", "psql"])
        # the safety copy of the *current* state exists and is named pre-restore
        copies = [p.name for p in self.backups.iterdir() if "pre-restore" in p.name]
        self.assertEqual(len(copies), 1)
        sql = "\n".join(self.statements())
        self.assertIn("pg_terminate_backend", sql)
        self.assertIn('DROP DATABASE IF EXISTS "investment";', sql)
        self.assertIn('CREATE DATABASE "investment" OWNER "investment";', sql)
        restore = next(r for r in self.docker.exec_log if "--no-owner" in r.args)
        self.assertIn("--role=investment", restore.args)
        self.assertEqual(restore.stdin_from, self.dump)

    def test_no_drop_when_the_safety_copy_fails(self) -> None:
        self.docker.exec_handler = lambda r: (
            ExecResult(1, b"", b"boom") if r.args[0] == "pg_dump" else None
        )
        self.ui.push("investment", False)  # type the name; then refuse to go on without a copy
        with self.assertRaises(UserAbort):
            self.ops.restore(self.INFO, self.dump)
        self.assertNotIn("DROP DATABASE", "\n".join(self.statements()))

    def test_failed_restore_reports_where_the_safety_copy_is(self) -> None:
        def handler(record: ExecRecord) -> ExecResult | None:
            if record.args[0] == "pg_dump":
                return ExecResult(0, b"data")
            if "--no-owner" in record.args:
                return ExecResult(1, b"", b"pg_restore: error")
            return None

        self.docker.exec_handler = handler
        self.ui.push("investment")
        with self.assertRaises(PostgresError) as ctx:
            self.ops.restore(self.INFO, self.dump)
        self.assertIn(str(self.backups), str(ctx.exception))


class RotateTests(OpsTestCase):
    def test_rotation_updates_role_vault_and_url(self) -> None:
        env_path = self.dir / ".env.production"
        env_path.write_text("DATABASE_URL='old'\nOTHER='keep'\n", encoding="utf-8")
        info = DatabaseInfo(
            "database-postgres-1", "shared-infra", "postgres", "investment", "investment"
        )
        self.ui.push("generate")
        self.ops.rotate_password(manifest(), info, env_path)
        env = read_env(env_path)
        self.assertEqual(env["OTHER"], "keep")
        password = env["DATABASE_URL"].split(":")[2].split("@")[0]
        self.assertEqual(self.vault.get("investment", "DB_PASSWORD"), password)
        sql = "\n".join(self.statements())
        self.assertIn('ALTER ROLE "investment" PASSWORD \'SCRAM-SHA-256$', sql)
        self.assertNotIn(password, sql)
        self.assertEqual(self.log.redactor.redact(password), "***")

    def test_env_is_untouched_when_the_role_change_fails(self) -> None:
        env_path = self.dir / ".env.production"
        env_path.write_text("DATABASE_URL='old'\n", encoding="utf-8")
        info = DatabaseInfo(
            "database-postgres-1", "shared-infra", "postgres", "investment", "investment"
        )

        def fail_alter(record: ExecRecord) -> ExecResult | None:
            if "ALTER ROLE" in record.sql:
                return ExecResult(1, b"", b"ERROR")
            return self.handle(record)

        self.docker.exec_handler = fail_alter
        self.ui.push("generate")
        with self.assertRaises(PostgresError):
            self.ops.rotate_password(manifest(), info, env_path)
        self.assertEqual(read_env(env_path)["DATABASE_URL"], "old")


class DockerFailureTests(unittest.TestCase):
    def test_precheck_reports_docker_errors(self) -> None:
        docker = FakeDocker()
        docker.fail.add("list_running_containers")
        ops = PostgresOps(docker, ScriptedUi(), Logger(None))
        self.assertIn("fake", ops.precheck(manifest())[0])
        self.assertTrue(isinstance(DockerError("x"), Exception))


if __name__ == "__main__":
    unittest.main()
