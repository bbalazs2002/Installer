"""Database backup files: naming, listing and retention (specification 7.3, 7.5).

A backup is ``<db>-<version>-<UTC timestamp>.dump`` where ``<version>`` is the version *whose
data it holds* (the version installed when it was made). Only files of that exact shape inside
the installation's own ``backups`` directory are ever listed or deleted, and every deletion is
logged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .logging_ import Logger
from .plan import BackupRef
from .ui.base import Item, Ui

RETENTION_DAYS = 365
PRE_RESTORE = "pre-restore"
BACKUP_FILE_MODE = 0o600
_VERSION_RE = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+?)?"
_NAME_RE = re.compile(
    rf"^(?P<db>[a-z][a-z0-9_]*)-(?P<ver>{PRE_RESTORE}|{_VERSION_RE})-"
    r"(?P<ts>\d{8}T\d{6}Z)\.dump$"
)


@dataclass(frozen=True)
class BackupFile:
    path: Path
    database: str
    version: str  # a SemVer string, or ``pre-restore``
    created: str  # ISO 8601 UTC, from the file name
    size: int
    modified: datetime


def backup_filename(database: str, version: str, now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{database}-{version}-{stamp}.dump"


def create_private_file(path: Path) -> None:
    """Create ``path`` empty with mode 600 from the start (never ``chmod`` afterwards)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, BACKUP_FILE_MODE)
    os.close(fd)


def list_backups(directory: Path, database: str | None = None) -> list[BackupFile]:
    """Backups in ``directory``, newest first. Symlinks and foreign files are ignored."""
    files: list[BackupFile] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        match = _NAME_RE.match(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        if database is not None and match["db"] != database:
            continue
        stat = path.stat()
        stamp = datetime.strptime(match["ts"], "%Y%m%dT%H%M%SZ")
        files.append(
            BackupFile(
                path=path,
                database=match["db"],
                version=match["ver"],
                created=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    return sorted(files, key=lambda f: f.created, reverse=True)


def backup_refs(directory: Path, database: str) -> list[BackupRef]:
    """The backups as the planner wants them (only real versions, not ``pre-restore``)."""
    return [
        BackupRef(version=f.version, path=str(f.path), created_at=f.created)
        for f in list_backups(directory, database)
        if f.version != PRE_RESTORE
    ]


def old_backups(
    files: Sequence[BackupFile], now: datetime, days: int = RETENTION_DAYS
) -> list[BackupFile]:
    """Files whose *modification time* is older than ``days`` days."""
    limit = now - timedelta(days=days)
    return [f for f in files if f.modified < limit]


def delete_backups(directory: Path, files: Sequence[BackupFile], log: Logger) -> int:
    """Delete the given backups. Returns how many were removed.

    Safety: only plain files directly inside ``directory`` whose name has the backup shape.
    """
    root = directory.resolve()
    removed = 0
    for item in files:
        path = item.path
        if (
            path.parent.resolve() != root
            or _NAME_RE.match(path.name) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            log.warning(
                f"Backup deletion skipped (not a regular file of the backup directory): {path}"
            )
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning(f"Deleting the backup {path.name} failed: {exc}")
            continue
        log.info(f"Old backup deleted: {path.name} ({item.size} bytes)")
        removed += 1
    return removed


def _megabytes(size: int) -> str:
    return f"{size / 2**20:.1f}"


def offer_retention(
    ui: Ui,
    log: Logger,
    directory: Path,
    *,
    auto_delete: bool,
    now: datetime,
    force_prompt: bool = False,
    database: str | None = None,
) -> bool:
    """Ask about deleting backups older than a year; returns the (possibly new) auto flag.

    With ``auto_delete`` set the deletion happens silently (but is logged), unless
    ``force_prompt`` (the manual menu entry). Choices: delete now / not now / always.
    """
    old = old_backups(list_backups(directory, database), now)
    if not old:
        return auto_delete
    if auto_delete and not force_prompt:
        delete_backups(directory, old, log)
        return True

    total = sum(f.size for f in old)
    choice = ui.menu(
        "Old database backups",
        [
            Item("now", "Delete now"),
            Item("later", "Not now"),
            Item("always", "Delete now, and always delete the ones older than a year"),
        ],
        text=f"{len(old)} backup(s) (total {_megabytes(total)} MB) are older than a year. Delete them?",
        default="now",
    )
    if choice == "later":
        return auto_delete
    delete_backups(directory, old, log)
    return True if choice == "always" else auto_delete


Clock = Callable[[], datetime]
