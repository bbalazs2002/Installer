"""Checking installed applications for newer releases (menu 2, ``telepito updates``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .errors import TelepitoError
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
        except TelepitoError as exc:
            result.append(UpdateInfo(record, error=str(exc)))
    return result


def format_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "ismeretlen dátum"
    seconds = (now - moment).total_seconds()
    if seconds < 3600:
        return "most"
    if seconds < 86400:
        return f"{int(seconds // 3600)} órája"
    days = int(seconds // 86400)
    return "ma" if days == 0 else f"{days} napja"


def format_update_line(info: UpdateInfo, now: datetime) -> str:
    record = info.record
    if info.error is not None:
        return f"{record.name}  {record.version}  (hiba: {info.error})"
    if info.latest is None:
        return f"{record.name}  {record.version}  (nincs kiadás)"
    latest = info.latest.version
    shown = str(latest) if latest is not None else info.latest.tag
    if not info.available:
        return f"{record.name}  {record.version}  (naprakész)"
    return (
        f"{record.name}  {record.version} → {shown}  ({format_age(info.latest.published_at, now)})"
    )
