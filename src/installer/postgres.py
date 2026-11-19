"""PostgreSQL support: discovery, database/role creation, backup, restore, password rotation.

Everything runs through ``docker exec`` into the chosen Postgres container, using the local
socket of the official image (``trust`` for local connections), so no administrator password is
ever asked for or stored. SQL always travels on **stdin** (never in arguments, which ``ps``
would show). Passwords are turned into SCRAM-SHA-256 verifiers on this side, so the server log
sees the verifier, not the password. Identifiers are validated and quoted; literals escaped.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .backup import PRE_RESTORE, backup_filename, create_private_file
from .docker import ContainerInfo, Docker, DockerError, ExecResult
from .envfile import generate_secret, update_env_file
from .errors import InstallerError, UserAbort
from .installer import DatabaseOps, DatabaseSetup
from .logging_ import Logger
from .manifest import DatabaseSpec, Manifest
from .state import DatabaseInfo
from .ui.base import Item, Ui, ask_validated
from .validators import is_db_identifier
from .vault import Vault, VaultError

PG_PORT = 5432
PREFERRED_NETWORK = "shared-infra"
SCRAM_ITERATIONS = 4096
SCRAM_PREFIX = "SCRAM-SHA-256"
PASSWORD_BYTES = 24
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
PG_EXEC_TIMEOUT = 300
DUMP_TIMEOUT = 3600
VAULT_PASSWORD_NAME = "DB_PASSWORD"

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_ADMIN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PLACEHOLDER_RE = re.compile(r"\{(user|password|host|port|database)\}")


class PostgresError(InstallerError):
    """A database operation failed. The message is user-facing and already masked."""


# ------------------------------------------------------------------------------ SCRAM


@dataclass(frozen=True)
class ScramKeys:
    client_key: bytes
    stored_key: bytes
    server_key: bytes


def scram_keys(password: str, salt: bytes, iterations: int = SCRAM_ITERATIONS) -> ScramKeys:
    """The SCRAM-SHA-256 keys of RFC 5802/7677 for ``password``.

    Passwords handled by the installer are plain printable ASCII, for which the SASLprep
    normalisation that PostgreSQL applies is the identity, so none is needed here.
    """
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return ScramKeys(
        client_key=client_key,
        stored_key=hashlib.sha256(client_key).digest(),
        server_key=hmac.new(salted, b"Server Key", hashlib.sha256).digest(),
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS
) -> str:
    """``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`` as PostgreSQL stores it."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    keys = scram_keys(password, salt, iterations)
    parts = (_b64(salt), _b64(keys.stored_key), _b64(keys.server_key))
    return f"{SCRAM_PREFIX}${iterations}:{parts[0]}${parts[1]}:{parts[2]}"


# --------------------------------------------------------------------------- quoting


def quote_ident(name: str) -> str:
    if "\x00" in name or not name:
        raise PostgresError("Invalid identifier.")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """A string literal. Callers also ``SET standard_conforming_strings = on`` first."""
    if "\x00" in value:
        raise PostgresError("Invalid value (NUL character).")
    return "'" + value.replace("'", "''") + "'"


def validate_password(password: str) -> str | None:
    """Passwords are limited to printable ASCII, which also keeps SCRAM and URLs simple."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return (
            f"The password must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters "
            "long."
        )
    if not all(0x21 <= ord(c) <= 0x7E for c in password):
        return "The password may contain only visible ASCII characters (no spaces)."
    return None


def build_url(
    template: str, *, user: str, password: str, host: str, port: int, database: str
) -> str:
    """Fill ``urlTemplate`` in one pass; user and password are URL-encoded."""
    values = {
        "user": quote(user, safe=""),
        "password": quote(password, safe=""),
        "host": host,
        "port": str(port),
        "database": quote(database, safe=""),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


# -------------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class PostgresContainer:
    name: str
    image: str
    networks: Mapping[str, tuple[str, ...]]  # network -> aliases
    admin_user: str


@dataclass(frozen=True)
class ServerChoice:
    container: PostgresContainer
    network: str
    host: str
    admin_user: str


def _is_postgres(container: ContainerInfo) -> bool:
    repository = container.image.split(":")[0].lower()
    if "postgres" in repository:
        return True
    has_env = any(key.startswith("POSTGRES_") for key in container.env)
    return "5432/tcp" in container.exposed_ports and has_env


def find_postgres_containers(docker: Docker) -> list[PostgresContainer]:
    found: list[PostgresContainer] = []
    for item in docker.list_running_containers():
        if _is_postgres(item):
            found.append(
                PostgresContainer(
                    name=item.name,
                    image=item.image,
                    networks=dict(item.networks),
                    admin_user=item.env.get("POSTGRES_USER") or "postgres",
                )
            )
    return found


def default_host(container: PostgresContainer, network: str) -> str:
    """The alias the application should use: the compose service name if there is one."""
    aliases = container.networks.get(network, ())
    candidates = [a for a in aliases if a != container.name and not _CONTAINER_ID_RE.match(a)]
    return candidates[0] if candidates else container.name


def choose_server(ui: Ui, containers: list[PostgresContainer], description: str) -> ServerChoice:
    """Interactive choice of container, network, host alias and admin user (7.1)."""
    if not containers:
        raise PostgresError(
            "No running Postgres container found. The installation needs a database"
            + (f": {description}" if description else ".")
        )
    if len(containers) == 1:
        container = containers[0]
        if not ui.confirm(
            f"Use this Postgres container?\n\n  {container.name} ({container.image})",
            title="Database server",
            default=True,
        ):
            raise UserAbort("Choosing the database server was cancelled.")
    else:
        preferred = next(
            (c.name for c in containers if PREFERRED_NETWORK in c.networks), containers[0].name
        )
        by_name = {c.name: c for c in containers}
        items = [
            Item(c.name, f"{c.name} ({c.image})  networks: {', '.join(c.networks) or '-'}")
            for c in containers
        ]
        chosen = ui.choose("Database server", items, text=description, default=preferred)
        container = by_name[chosen]

    networks = list(container.networks)
    if not networks:
        raise PostgresError(f"The container {container.name} is not on any network.")
    if len(networks) == 1:
        network = networks[0]
    else:
        default = PREFERRED_NETWORK if PREFERRED_NETWORK in networks else networks[0]
        network = ui.choose(
            "Network",
            [Item(n, n) for n in networks],
            text="Which network does the application use to reach the database?",
            default=default,
        )
    host = ask_validated(
        ui,
        "The database host name for the application (network alias):",
        lambda v: None if _HOST_RE.match(v) else "Invalid host name.",
        title="Database server",
        default=default_host(container, network),
    )
    admin = ask_validated(
        ui,
        "The database administrator user (no password is asked for):",
        lambda v: None if _ADMIN_RE.match(v) else "Invalid user name.",
        title="Database server",
        default=container.admin_user,
    )
    return ServerChoice(container, network, host, admin)


# ------------------------------------------------------------------------- psql client


class PsqlClient:
    """Runs SQL as the admin user inside one container. SQL goes to stdin only."""

    def __init__(self, docker: Docker, container: str, admin: str) -> None:
        self._docker = docker
        self.container = container
        self.admin = admin

    def run(self, sql: str, *, database: str = "postgres") -> str:
        args = [
            "psql",
            "-U",
            self.admin,
            "-d",
            database,
            "-X",
            "-q",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
        ]
        statement = "SET standard_conforming_strings = on;\n" + sql
        result = self._exec(args, statement.encode("utf-8"))
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _exec(self, args: list[str], stdin: bytes) -> ExecResult:
        try:
            result = self._docker.exec(self.container, args, stdin=stdin, timeout=PG_EXEC_TIMEOUT)
        except DockerError as exc:
            raise PostgresError(str(exc)) from exc
        if result.returncode != 0:
            raise PostgresError(_tail(result.stderr) or "psql exited with an error.")
        return result

    def database_exists(self, name: str) -> bool:
        return bool(self.run(f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(name)};"))

    def role_exists(self, name: str) -> bool:
        return bool(self.run(f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(name)};"))

    def password_encryption(self) -> str:
        return self.run("SHOW password_encryption;")

    def check_login(self, host: str, database: str, user: str, password: str) -> None:
        """Log in over the network address (SCRAM is enforced there, unlike on loopback).

        The password reaches the container only through stdin, into a private ``.pgpass``
        file that is deleted again right away.
        """
        path = f"/tmp/.installer-pgpass-{secrets.token_hex(6)}"
        fields = (host, str(PG_PORT), database, user, password)
        line = ":".join(f.replace("\\", "\\\\").replace(":", "\\:") for f in fields) + "\n"
        try:
            self._exec(["sh", "-c", f"umask 077; cat > {path}"], line.encode("utf-8"))
            try:
                login = [
                    "env", f"PGPASSFILE={path}",
                    "psql", "-w", "-h", host, "-p", str(PG_PORT), "-U", user, "-d", database,
                    "-X", "-q", "-tA", "-c", "SELECT 1",
                ]  # fmt: skip
                self._exec(login, b"")
            finally:
                try:
                    self._docker.exec(self.container, ["rm", "-f", path])
                except DockerError:
                    pass
        except PostgresError as exc:
            raise PostgresError(f"The login test failed ({user}@{host}/{database}): {exc}") from exc


def _tail(data: bytes, lines: int = 3) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return "\n".join(text.splitlines()[-lines:])


def password_clause(password: str, encryption: str) -> str:
    """The ``PASSWORD '...'`` value: a SCRAM verifier when the server uses SCRAM."""
    if encryption == "scram-sha-256":
        return quote_literal(scram_sha256_verifier(password))
    return quote_literal(password)


# ------------------------------------------------------------------------- the database ops


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresOps(DatabaseOps):
    def __init__(
        self,
        docker: Docker,
        ui: Ui,
        log: Logger,
        *,
        vault_provider: Callable[[], Vault | None] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._docker = docker
        self._ui = ui
        self._log = log
        self._vault_provider = vault_provider
        self._clock = clock

    # ---------------------------------------------------------------------- precheck

    def precheck(self, manifest: Manifest) -> list[str]:
        spec = manifest.database
        if spec is None:
            return []
        try:
            containers = find_postgres_containers(self._docker)
        except DockerError as exc:
            return [str(exc)]
        if containers:
            return []
        reason = f": {spec.description}" if spec.description else "."
        return [f"No running Postgres container found. The installation needs a database{reason}"]

    # ------------------------------------------------------------------------ create

    def create(self, manifest: Manifest) -> DatabaseSetup:
        spec = manifest.database
        assert spec is not None
        ui = self._ui
        if spec.description:
            ui.message(spec.description, title="Why is a database needed?")
        server = choose_server(ui, find_postgres_containers(self._docker), spec.description)
        client = PsqlClient(self._docker, server.container.name, server.admin_user)

        database, user, password, create_role, create_db = self._decide_names(
            ui, client, spec, manifest.name
        )
        self._log.redactor.register(password)
        encryption = client.password_encryption()
        if encryption != "scram-sha-256":
            ui.message(
                f"The server's password encryption is: {encryption or 'unknown'}. The password "
                "will be sent as plain text in the CREATE ROLE command, which may end up in "
                "the server log.",
                title="Warning",
            )

        self._provision(client, encryption, database, user, password, create_role, create_db)
        client.check_login(server.host, database, user, password)
        self._save_password(manifest.name, password)

        url = build_url(
            spec.url_template,
            user=user,
            password=password,
            host=server.host,
            port=PG_PORT,
            database=database,
        )
        self._log.redactor.register(url)
        info = DatabaseInfo(
            container=server.container.name,
            network=server.network,
            host=server.host,
            name=database,
            user=user,
            admin_user=server.admin_user,
        )
        self._log.info(f"Database ready: {database} ({user}) in container {info.container}.")
        return DatabaseSetup(info, {spec.url_env: url})

    def _decide_names(
        self, ui: Ui, client: PsqlClient, spec: DatabaseSpec, app_name: str
    ) -> tuple[str, str, str, bool, bool]:
        """Ask for database, user and password; resolve clashes with existing objects."""
        database, user = spec.default_database, spec.default_user
        while True:
            database = ask_validated(
                ui,
                "The database name:",
                lambda v: None if is_db_identifier(v) else _IDENT_HELP,
                title="Database",
                default=database,
            )
            user = ask_validated(
                ui,
                "The database user:",
                lambda v: None if is_db_identifier(v) else _IDENT_HELP,
                title="Database",
                default=user,
            )
            db_exists = client.database_exists(database)
            role_exists = client.role_exists(user)
            if not db_exists and not role_exists:
                return database, user, self._ask_new_password(), True, True
            existing = [
                *(["database"] if db_exists else []),
                *(["user"] if role_exists else []),
            ]
            choice = ui.menu(
                "Already exists",
                [
                    Item("rename", "I will give another name"),
                    Item("use", "Use the existing one"),
                ],
                text=f"Already exists: {' and '.join(existing)} ({database} / {user}).",
            )
            if choice == "rename":
                continue
            if role_exists:
                password = ask_validated(
                    ui,
                    f"Password of the user {user} (it will be verified):",
                    lambda v: validate_password(v),
                    title="Database",
                    secret=True,
                )
            else:
                password = self._ask_new_password()
            return database, user, password, not role_exists, not db_exists

    def _ask_new_password(self) -> str:
        ui = self._ui
        choice = ui.menu(
            "Database password",
            [Item("generate", "Generate (recommended)"), Item("manual", "Type it in (hidden)")],
        )
        if choice == "generate":
            return generate_secret(PASSWORD_BYTES)
        while True:
            first = ask_validated(
                ui, "Password:", lambda v: validate_password(v), title="Database", secret=True
            )
            if ui.secret("Password again:", title="Database") == first:
                return first
            ui.message("The two passwords do not match.", title="Database")

    def _provision(
        self,
        client: PsqlClient,
        encryption: str,
        database: str,
        user: str,
        password: str,
        create_role: bool,
        create_db: bool,
    ) -> None:
        q_user, q_db = quote_ident(user), quote_ident(database)
        if create_role:
            clause = password_clause(password, encryption)
            client.run(f"CREATE ROLE {q_user} LOGIN PASSWORD {clause};")
        if create_db:
            try:
                client.run(f"CREATE DATABASE {q_db} OWNER {q_user};")
            except PostgresError:
                if create_role:  # do not leave our half-made role behind
                    try:
                        client.run(f"DROP ROLE IF EXISTS {q_user};")
                    except PostgresError as cleanup:
                        self._log.warning(f"Removing the user failed: {cleanup}")
                raise
        client.run(f"GRANT ALL ON SCHEMA public TO {q_user};", database=database)

    def _save_password(self, installation: str, password: str) -> None:
        if self._vault_provider is None:
            return
        try:
            vault = self._vault_provider()
            if vault is None:
                self._ui.message(
                    "The database password was not saved to the vault; only .env.production "
                    "contains it.",
                    title="Vault",
                )
                return
            vault.set(installation, VAULT_PASSWORD_NAME, password)
            vault.save()
        except VaultError as exc:
            self._ui.message(
                f"Saving the database password to the vault failed ({exc}).",
                title="Vault",
            )

    # ------------------------------------------------------------------------ backup

    def backup(self, info: DatabaseInfo, version: str, backups_dir: Path) -> Path | None:
        """Dump the database into ``backups_dir`` (mode 600, verified readable).

        On failure the user decides whether to go on without a backup: ``None`` means "yes,
        without"; declining raises :class:`UserAbort`.
        """
        backups_dir.mkdir(parents=True, exist_ok=True)
        path = backups_dir / backup_filename(info.name, version, self._clock())
        create_private_file(path)
        try:
            dump = self._docker.exec(
                info.container,
                ["pg_dump", "-U", info.admin_user, "-Fc", info.name],
                stdout_to=path,
                timeout=DUMP_TIMEOUT,
            )
            if dump.returncode != 0:
                raise PostgresError(_tail(dump.stderr) or "pg_dump exited with an error.")
            if path.stat().st_size == 0:
                raise PostgresError("The backup is empty.")
            check = self._docker.exec(
                info.container, ["pg_restore", "--list"], stdin_from=path, timeout=PG_EXEC_TIMEOUT
            )
            if check.returncode != 0:
                raise PostgresError("The backup cannot be read back (pg_restore --list).")
        except (PostgresError, DockerError) as exc:
            path.unlink(missing_ok=True)
            self._log.error(f"Database backup failed: {exc}")
            self._ui.message(f"The database backup failed: {exc}", title="Backup")
            if self._ui.confirm(
                "Continue without a backup? (If something goes wrong there will be nothing to roll back to.)",
                title="Backup",
                default=False,
            ):
                return None
            raise UserAbort("Without a backup the operation stops.") from exc
        self._log.info(f"Database backup done: {path.name} ({path.stat().st_size} bytes)")
        return path

    # ----------------------------------------------------------------------- restore

    def restore(
        self, info: DatabaseInfo, backup: Path, prepare: Callable[[], None] | None = None
    ) -> None:
        """Replace the database with ``backup``. Needs the database name typed in full."""
        ui = self._ui
        typed = ui.text(
            f"The contents of the database {info.name} will be OVERWRITTEN with the backup {backup.name}. "
            f"To continue, type the database name: {info.name}",
            title="Restore the database",
        )
        if typed.strip() != info.name:
            raise UserAbort("The name does not match; the restore was cancelled.")

        # A safety net first: the current state, so the restore itself can be undone.
        self.backup(info, PRE_RESTORE, backup.parent)
        if prepare is not None:
            prepare()

        client = PsqlClient(self._docker, info.container, info.admin_user)
        q_db, q_user = quote_ident(info.name), quote_ident(info.user)
        client.run(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {quote_literal(info.name)} AND pid <> pg_backend_pid();\n"
            f"DROP DATABASE IF EXISTS {q_db};\n"
            f"CREATE DATABASE {q_db} OWNER {q_user};"
        )
        try:
            result = self._docker.exec(
                info.container,
                [
                    "pg_restore",
                    "-U",
                    info.admin_user,
                    "-d",
                    info.name,
                    "--no-owner",
                    f"--role={info.user}",
                    "--exit-on-error",
                ],  # fmt: skip
                stdin_from=backup,
                timeout=DUMP_TIMEOUT,
            )
        except DockerError as exc:
            raise PostgresError(str(exc)) from exc
        if result.returncode != 0:
            raise PostgresError(
                "The restore failed: "
                + (_tail(result.stderr) or "pg_restore error")
                + f". The state before the restore was saved in: {backup.parent}"
            )
        client.run(f"GRANT ALL ON SCHEMA public TO {q_user};", database=info.name)
        self._log.info(f"The database was restored from the backup {backup.name}.")

    # ----------------------------------------------------------------------- rotation

    def rotate_password(self, manifest: Manifest, info: DatabaseInfo, env_path: Path) -> None:
        """New password: vault first (so it cannot be lost), then the role, then the URL."""
        spec = manifest.database
        assert spec is not None
        password = self._ask_new_password()
        self._log.redactor.register(password)
        self._save_password(manifest.name, password)

        client = PsqlClient(self._docker, info.container, info.admin_user)
        clause = password_clause(password, client.password_encryption())
        client.run(f"ALTER ROLE {quote_ident(info.user)} PASSWORD {clause};")
        client.check_login(info.host, info.name, info.user, password)
        url = build_url(
            spec.url_template,
            user=info.user,
            password=password,
            host=info.host,
            port=PG_PORT,
            database=info.name,
        )
        self._log.redactor.register(url)
        update_env_file(env_path, {spec.url_env: url})
        self._log.info(f"The database password of {info.user} changed.")


_IDENT_HELP = (
    "Must start with a lowercase letter; lowercase letters, digits and underscores only (max 63)."
)
