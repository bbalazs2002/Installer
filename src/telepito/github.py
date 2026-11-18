"""The GitHub access interface and its data types.

T.0 defines only the interface. The real implementation (token in the ``Authorization``
header only, redirect handling that drops it, ETag caching, retries) arrives in T.1,
together with a local fake GitHub server for tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import TelepitoError
from .versions import Version


class GitHubError(TelepitoError):
    """Base class for GitHub failures. Messages are user-facing and never contain the token."""


class GitHubUnavailable(GitHubError):
    """Network failure, timeout or 5xx after retries (\"GitHub nem elérhető\")."""


class GitHubAuthError(GitHubError):
    """Missing, invalid or expired token (\"A token lejárt\")."""


class GitHubAccessDenied(GitHubError):
    """The token has no access to the repository (\"A repóhoz nincs jog\")."""


class GitHubRateLimited(GitHubError):
    """403/429 with a ``Retry-After``; ``retry_after`` is in seconds when known."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    prerelease: bool
    published_at: datetime | None
    body: str
    assets: tuple[Asset, ...]

    @property
    def version(self) -> Version | None:
        """The parsed SemVer of the tag, or ``None`` if the tag is not SemVer."""
        return Version.try_parse(self.tag)

    def asset(self, name: str) -> Asset | None:
        for item in self.assets:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class TokenStatus:
    """Result of a token check: when it expires, if GitHub told us."""

    expires_at: datetime | None


# Called with (bytes_done, bytes_total); total is 0 when unknown.
ProgressCallback = Callable[[int, int], None]


class GitHub(ABC):
    @abstractmethod
    def check_access(self, repo: str) -> TokenStatus:
        """``GET /repos/{o}/{r}``; raises the specific :class:`GitHubError` on failure."""

    @abstractmethod
    def list_releases(self, repo: str) -> list[Release]:
        """All non-draft releases, newest first (paginated internally)."""

    @abstractmethod
    def latest_stable(self, repo: str) -> Release | None:
        """The newest non-prerelease release, or ``None``."""

    @abstractmethod
    def download_asset(
        self,
        repo: str,
        asset: Asset,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Download one release asset to ``destination``."""
