"""Persistent state: ``repos.json``, ``installs.json`` (server-wide) and ``.install.json``.

None of these files may ever contain a secret. ``DatabaseInfo`` deliberately has no
password field, and unknown keys are dropped when reading so a stray secret is never
carried over.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InstallerError
from .fsutil import atomic_write_text
from .validators import is_app_name, is_repo

SCHEMA_VERSION = 1
INSTALL_INFO_NAME = ".install.json"
STATE_FILE_MODE = 0o640


class StateError(InstallerError):
    """A state file is unreadable or has an unexpected shape. Never guessed around."""


def _load_json(path: Path) -> Any | None:
    """Return the decoded JSON, or ``None`` if the file does not exist."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError(f"{path} cannot be read: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise StateError(f"{path} is not valid JSON: {exc}") from exc


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _check_schema(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StateError(f"The format of {path} is unknown (an object was expected).")
    version = data.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError(f"The schemaVersion field of {path} is missing or invalid.")
    if version > SCHEMA_VERSION:
        raise StateError(
            f"{path} has a newer format ({version}) than this installer knows. "
            "Update the installer."
        )
    return data


def _str(data: dict[str, Any], key: str, path: Path, *, optional: bool = False) -> str | None:
    value = data.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise StateError(f"In {path} the field {key} is missing or invalid.")
    return value


def _str_list(data: dict[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise StateError(f"In {path} the field {key} must be a list of strings.")
    return list(value)


# --------------------------------------------------------------------------- repos.json


@dataclass(frozen=True)
class KnownRepo:
    repo: str
    default_path: str | None = None


def load_repos(path: Path) -> list[KnownRepo]:
    """Read ``repos.json``; a missing file means an empty list."""
    raw = _load_json(path)
    if raw is None:
        return []
    data = _check_schema(raw, path)
    items = data.get("repos", [])
    if not isinstance(items, list):
        raise StateError(f"The repos field of {path} must be a list.")
    result: list[KnownRepo] = []
    for item in items:
        if not isinstance(item, dict):
            raise StateError(f"{path} contains an item that is not an object.")
        repo = _str(item, "repo", path)
        assert repo is not None
        if not is_repo(repo):
            raise StateError(f"Invalid repository in {path}: {repo!r}")
        default_path = _str(item, "defaultPath", path, optional=True)
        result.append(KnownRepo(repo, default_path))
    return result


def save_repos(path: Path, repos: list[KnownRepo]) -> None:
    items = []
    for known in repos:
        entry: dict[str, Any] = {"repo": known.repo}
        if known.default_path:
            entry["defaultPath"] = known.default_path
        items.append(entry)
    atomic_write_text(
        path, _dump({"schemaVersion": SCHEMA_VERSION, "repos": items}), STATE_FILE_MODE
    )


# ------------------------------------------------------------------------ installs.json


@dataclass(frozen=True)
class InstallRecord:
    """One line of the server-wide registry (no secrets)."""

    name: str
    repo: str
    path: str
    version: str
    installed_at: str


def _record_from_dict(item: Any, path: Path) -> InstallRecord:
    if not isinstance(item, dict):
        raise StateError(f"{path} contains an item that is not an object.")
    name = _str(item, "name", path)
    repo = _str(item, "repo", path)
    install_path = _str(item, "path", path)
    version = _str(item, "version", path)
    installed_at = _str(item, "installedAt", path)
    assert name and repo and install_path and version and installed_at
    if not is_app_name(name) or not is_repo(repo):
        raise StateError(f"Invalid name or repository in {path}: {name!r}, {repo!r}")
    return InstallRecord(name, repo, install_path, version, installed_at)


def load_installs(path: Path) -> list[InstallRecord]:
    """Read ``installs.json``; a missing file means no installations."""
    raw = _load_json(path)
    if raw is None:
        return []
    data = _check_schema(raw, path)
    items = data.get("installs", [])
    if not isinstance(items, list):
        raise StateError(f"The installs field of {path} must be a list.")
    return [_record_from_dict(item, path) for item in items]


def save_installs(path: Path, records: list[InstallRecord]) -> None:
    items = [
        {
            "name": r.name,
            "repo": r.repo,
            "path": r.path,
            "version": r.version,
            "installedAt": r.installed_at,
        }
        for r in records
    ]
    atomic_write_text(
        path, _dump({"schemaVersion": SCHEMA_VERSION, "installs": items}), STATE_FILE_MODE
    )


def upsert_install(records: list[InstallRecord], record: InstallRecord) -> list[InstallRecord]:
    """Return a new list where the record with the same ``name`` is replaced (or appended)."""
    result = [r for r in records if r.name != record.name]
    result.append(record)
    return sorted(result, key=lambda r: r.name)


def find_install(records: list[InstallRecord], name: str) -> InstallRecord | None:
    for record in records:
        if record.name == name:
            return record
    return None


# ------------------------------------------------------------------------ .install.json


@dataclass(frozen=True)
class DatabaseInfo:
    """Where the application's database lives. Never contains the password."""

    container: str
    network: str
    host: str
    name: str
    user: str
    admin_user: str = "postgres"


@dataclass(frozen=True)
class InstallInfo:
    """The per-installation record (``<target>/.install.json``)."""

    name: str
    repo: str
    version: str
    installed_at: str
    previous_versions: tuple[str, ...] = ()
    migrations: tuple[str, ...] = ()
    database: DatabaseInfo | None = None
    port: int | None = None
    auto_delete_old_backups: bool = False
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "name": self.name,
            "repo": self.repo,
            "version": self.version,
            "installedAt": self.installed_at,
            "previousVersions": list(self.previous_versions),
            "migrations": list(self.migrations),
            "autoDeleteOldBackups": self.auto_delete_old_backups,
        }
        if self.port is not None:
            data["port"] = self.port
        if self.database is not None:
            db = self.database
            data["database"] = {
                "container": db.container,
                "network": db.network,
                "host": db.host,
                "name": db.name,
                "user": db.user,
                "adminUser": db.admin_user,
            }
        return data


def install_info_path(target: Path) -> Path:
    return target / INSTALL_INFO_NAME


def parse_install_info(raw: Any, path: Path) -> InstallInfo:
    data = _check_schema(raw, path)
    name = _str(data, "name", path)
    repo = _str(data, "repo", path)
    version = _str(data, "version", path)
    installed_at = _str(data, "installedAt", path)
    assert name and repo and version and installed_at
    if not is_app_name(name) or not is_repo(repo):
        raise StateError(f"Invalid name or repository in {path}.")

    database: DatabaseInfo | None = None
    raw_db = data.get("database")
    if raw_db is not None:
        if not isinstance(raw_db, dict):
            raise StateError(f"The database field of {path} is not an object.")
        fields = {
            key: _str(raw_db, key, path) for key in ("container", "network", "host", "name", "user")
        }
        admin = _str(raw_db, "adminUser", path, optional=True) or "postgres"
        assert all(fields.values())
        database = DatabaseInfo(
            container=fields["container"] or "",
            network=fields["network"] or "",
            host=fields["host"] or "",
            name=fields["name"] or "",
            user=fields["user"] or "",
            admin_user=admin,
        )

    port = data.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise StateError(f"The port field of {path} is invalid.")
    auto_delete = data.get("autoDeleteOldBackups", False)
    if not isinstance(auto_delete, bool):
        raise StateError(f"The autoDeleteOldBackups field of {path} is invalid.")

    return InstallInfo(
        name=name,
        repo=repo,
        version=version,
        installed_at=installed_at,
        previous_versions=tuple(_str_list(data, "previousVersions", path)),
        migrations=tuple(_str_list(data, "migrations", path)),
        database=database,
        port=port,
        auto_delete_old_backups=auto_delete,
    )


def load_install_info(target: Path) -> InstallInfo | None:
    """Read ``<target>/.install.json``.

    Returns ``None`` if there is no such file. An unreadable or unrecognised file raises
    :class:`StateError`, because the planner must block rather than guess.
    """
    path = install_info_path(target)
    raw = _load_json(path)
    if raw is None:
        return None
    return parse_install_info(raw, path)


def save_install_info(target: Path, info: InstallInfo) -> None:
    atomic_write_text(install_info_path(target), _dump(info.to_dict()), STATE_FILE_MODE)


def is_effectively_empty(target: Path) -> bool:
    """True if ``target`` does not exist or is an empty directory."""
    try:
        with os.scandir(target) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True


@dataclass(frozen=True)
class TargetState:
    """What the target directory looks like right now (input of the planner)."""

    exists: bool
    empty: bool
    install_info: InstallInfo | None = None
    # Set when ``.install.json`` exists but cannot be understood; the planner then blocks.
    unreadable_reason: str | None = None


def inspect_target(target: Path) -> TargetState:
    """Look at ``target`` (read-only) and describe it."""
    exists = target.is_dir()
    empty = is_effectively_empty(target)
    try:
        info = load_install_info(target)
    except StateError as exc:
        return TargetState(exists, empty, None, str(exc))
    return TargetState(exists, empty, info)
