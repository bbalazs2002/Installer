"""Backup files of an installation: naming, listing, grouping and retention.

Two kinds of files live in an installation's ``backups`` directory:

* ``<db>-<version>-<UTC timestamp>.dump``        a database dump
* ``<volume>-<version>-<UTC timestamp>.tar.gz``  a compressed archive of a data volume

``<version>`` is the version *whose data it holds* (the version installed when it was made).
All the files of one backup run share the timestamp and the version and form a *backup set*.

Retention: a file older than a year is deleted, **except** the newest file of its series (the
database dumps are one series, every volume is another), so something is always left to
restore from, however old. Only files of exactly these shapes inside the installation's own
``backups`` directory are ever listed or deleted, and every deletion is logged.
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
_VOLUME_NAME_RE = re.compile(
    rf"^(?P<db>[a-z][a-z0-9_]*)-(?P<ver>{PRE_RESTORE}|{_VERSION_RE})-"
    r"(?P<ts>\d{8}T\d{6}Z)\.tar\.gz$"
)
KIND_DATABASE = "db"
KIND_VOLUME = "volume"


@dataclass(frozen=True)
class BackupFile:
    path: Path
    database: str  # the database name, or the volume name for a volume archive
    version: str  # a SemVer string, or ``pre-restore``
    created: str  # ISO 8601 UTC, from the file name
    size: int
    modified: datetime
    kind: str = KIND_DATABASE

    @property
    def series(self) -> str:
        """Files of one series replace each other over time (see the retention rule)."""
        return f"{self.kind}:{self.database}"


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_filename(database: str, version: str, now: datetime) -> str:
    return f"{database}-{version}-{_stamp(now)}.dump"


def volume_backup_filename(volume: str, version: str, now: datetime) -> str:
    return f"{volume}-{version}-{_stamp(now)}.tar.gz"


def create_private_file(path: Path) -> None:
    """Create ``path`` empty with mode 600 from the start (never ``chmod`` afterwards)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, BACKUP_FILE_MODE)
    os.close(fd)


def _list(
    directory: Path, pattern: re.Pattern[str], kind: str, name: str | None
) -> list[BackupFile]:
    files: list[BackupFile] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        match = pattern.match(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        if name is not None and match["db"] != name:
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
                kind=kind,
            )
        )
    return sorted(files, key=lambda f: f.created, reverse=True)


def list_backups(directory: Path, database: str | None = None) -> list[BackupFile]:
    """Database dumps in ``directory``, newest first. Symlinks and foreign files are ignored."""
    return _list(directory, _NAME_RE, KIND_DATABASE, database)


def list_volume_backups(directory: Path, volume: str | None = None) -> list[BackupFile]:
    """Volume archives in ``directory``, newest first."""
    return _list(directory, _VOLUME_NAME_RE, KIND_VOLUME, volume)


def list_all_backups(directory: Path, database: str | None = None) -> list[BackupFile]:
    """Database dumps (of ``database`` only, if given) and all volume archives, newest first."""
    files = list_backups(directory, database) + list_volume_backups(directory)
    return sorted(files, key=lambda f: f.created, reverse=True)


@dataclass(frozen=True)
class BackupSet:
    """The files of one backup run: same timestamp and version."""

    created: str
    version: str
    files: tuple[BackupFile, ...]

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def part(self, series: str) -> BackupFile | None:
        return next((f for f in self.files if f.series == series), None)

    def label(self) -> str:
        names = ", ".join(
            ("db " if f.kind == KIND_DATABASE else "") + f.database for f in self.files
        )
        return f"{self.version:<12} {self.created}  {self.size / 2**20:.1f} MB  [{names}]"


def group_sets(files: Sequence[BackupFile]) -> list[BackupSet]:
    """Group files into backup sets, newest first."""
    grouped: dict[tuple[str, str], list[BackupFile]] = {}
    for item in files:
        grouped.setdefault((item.created, item.version), []).append(item)
    sets = [
        BackupSet(created, version, tuple(sorted(members, key=lambda f: (f.kind, f.database))))
        for (created, version), members in grouped.items()
    ]
    return sorted(sets, key=lambda s: s.created, reverse=True)


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
    """Files to delete: *modified* more than ``days`` days ago, except the newest of a series.

    The newest file of each series (the database dumps; each volume) is always kept, however
    old it is, so there is always something to restore from.
    """
    newest: dict[str, BackupFile] = {}
    for item in files:
        best = newest.get(item.series)
        if best is None or (item.created, item.modified) > (best.created, best.modified):
            newest[item.series] = item
    limit = now - timedelta(days=days)
    return [f for f in files if f.modified < limit and newest[f.series].path != f.path]


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
            or (_NAME_RE.match(path.name) is None and _VOLUME_NAME_RE.match(path.name) is None)
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

    Database dumps and volume archives are handled together; the newest file of each series
    is never offered. With ``auto_delete`` set the deletion happens silently (but is logged),
    unless ``force_prompt`` (the manual menu entry). Choices: delete now / not now / always.
    """
    old = old_backups(list_all_backups(directory, database), now)
    if not old:
        return auto_delete
    if auto_delete and not force_prompt:
        delete_backups(directory, old, log)
        return True

    total = sum(f.size for f in old)
    choice = ui.menu(
        "Old backups",
        [
            Item("now", "Delete now"),
            Item("later", "Not now"),
            Item("always", "Delete now, and always delete the ones older than a year"),
        ],
        text=(
            f"{len(old)} backup(s) (total {_megabytes(total)} MB) are older than a year. "
            "Delete them?\n(The newest backup of the database and of each volume is always kept.)"
        ),
        default="now",
    )
    if choice == "later":
        return auto_delete
    delete_backups(directory, old, log)
    return True if choice == "always" else auto_delete


Clock = Callable[[], datetime]
