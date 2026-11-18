"""SemVer 2.0 parsing and ordering. Build metadata is parsed but ignored in comparisons."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import TelepitoError

_IDENT = r"[0-9A-Za-z-]+"
_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+(?P<build>{_IDENT}(?:\.{_IDENT})*))?$"
)
_NUMERIC_RE = re.compile(r"^[0-9]+$")


class VersionError(TelepitoError, ValueError):
    """Raised for text that is not a valid SemVer version."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        """Parse ``1.2.3``, ``v1.2.3``, ``1.2.3-rc.1``, ``1.2.3+build``."""
        match = _SEMVER_RE.match(text.strip()) if isinstance(text, str) else None
        if match is None:
            raise VersionError(f"Érvénytelen verzió: {text!r}")
        pre = tuple(match["pre"].split(".")) if match["pre"] else ()
        build = tuple(match["build"].split(".")) if match["build"] else ()
        for ident in pre:
            # Numeric prerelease identifiers must not have leading zeros.
            if _NUMERIC_RE.match(ident) and len(ident) > 1 and ident.startswith("0"):
                raise VersionError(f"Érvénytelen verzió: {text!r}")
        return cls(int(match["major"]), int(match["minor"]), int(match["patch"]), pre, build)

    @classmethod
    def try_parse(cls, text: str) -> Version | None:
        try:
            return cls.parse(text)
        except VersionError:
            return None

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def tag(self) -> str:
        """The git tag form (``v1.2.3``)."""
        return f"v{self}"

    @property
    def image_tag(self) -> str:
        """The Docker image tag form: the version without a leading ``v``."""
        return str(self)

    def _key(self) -> tuple[object, ...]:
        # A version without a prerelease is greater than one with it (flag 1 vs 0).
        # Identifiers: numeric ones sort before alphanumeric and compare as integers.
        pre = tuple((0, int(i), "") if _NUMERIC_RE.match(i) else (1, 0, i) for i in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __le__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() <= other._key()

    def __gt__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() > other._key()

    def __ge__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() >= other._key()

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text
