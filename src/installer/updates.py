"""Checking installed applications for newer releases (menu 2, ``installer updates``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .errors import InstallerError
from .github import GitHub, Release
from .state import InstallRecord
from .versions import Version


@dataclass(frozen=True)
class UpdateInfo:
    record: InstallRecord
    latest: Release | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        if self.latest is None or self.latest.version is None:
            return False
        current = Version.try_parse(self.record.version)
        return current is not None and self.latest.version > current


def find_updates(github: GitHub, records: Sequence[InstallRecord]) -> list[UpdateInfo]:
    """The newest *stable* release for each installation. One failing repo never hides the rest."""
    result: list[UpdateInfo] = []
    for record in records:
        try:
            result.append(UpdateInfo(record, github.latest_stable(record.repo)))
        except InstallerError as exc:
            result.append(UpdateInfo(record, error=str(exc)))
    return result


def format_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "unknown date"
    seconds = (now - moment).total_seconds()
    if seconds < 3600:
        return "just now"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    days = int(seconds // 86400)
    return "today" if days == 0 else f"{days} days ago"


def format_update_line(info: UpdateInfo, now: datetime) -> str:
    record = info.record
    if info.error is not None:
        return f"{record.name}  {record.version}  (error: {info.error})"
    if info.latest is None:
        return f"{record.name}  {record.version}  (no release)"
    latest = info.latest.version
    shown = str(latest) if latest is not None else info.latest.tag
    if not info.available:
        return f"{record.name}  {record.version}  (up to date)"
    return (
        f"{record.name}  {record.version} → {shown}  ({format_age(info.latest.published_at, now)})"
    )
