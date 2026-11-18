"""The real GitHub client, built on ``urllib`` (standard library only).

Security properties (see the specification, 8.2):

* the token is sent only in the ``Authorization`` header, never in a URL, a log line or an
  exception message;
* on a redirect to a different host (asset downloads go to object storage) the header is
  dropped, and an https -> http downgrade is refused;
* ``Link: rel="next"`` URLs are followed only on the API host, so pagination can never leak
  the token elsewhere.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .github import (
    Asset,
    GitHub,
    GitHubAccessDenied,
    GitHubAuthError,
    GitHubError,
    GitHubRateLimited,
    GitHubUnavailable,
    ProgressCallback,
    Release,
    TokenStatus,
)
from .validators import is_repo

API_BASE = "https://api.github.com"
USER_AGENT = "telepito"
API_VERSION = "2022-11-28"

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_ATTEMPTS = 4
BACKOFF_BASE = 0.5
MAX_RETRY_WAIT = 60.0  # a longer Retry-After is reported instead of waited for
CACHE_TTL = 600.0  # seconds (10 minutes)
MAX_JSON_BYTES = 20 * 1024 * 1024
MAX_PAGES = 50
CHUNK = 64 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,255}$")
_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

_MSG_UNAVAILABLE = "A GitHub nem elérhető (hálózati hiba vagy időtúllépés). Próbáld újra később."
_MSG_AUTH = "A GitHub-token érvénytelen vagy lejárt. Add meg újra a Beállítások menüben."
_MSG_DENIED = "A repó nem található, vagy a tokennek nincs hozzá jogosultsága."


class _Retry(Exception):
    """Internal: this attempt failed in a retryable way."""

    def __init__(self, error: GitHubError, delay: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.delay = delay


def _timed_connection(base: type[http.client.HTTPConnection], read_timeout: float) -> Any:
    """A connection class: connect timeout from the request, then a longer read timeout."""

    class _Connection(base):  # type: ignore[valid-type, misc]
        def connect(self) -> None:
            super().connect()
            if self.sock is not None:
                self.sock.settimeout(read_timeout)

    return _Connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, read_timeout: float) -> None:
        super().__init__()
        self._connection = _timed_connection(http.client.HTTPConnection, read_timeout)

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, read_timeout: float) -> None:
        super().__init__()
        self._connection = _timed_connection(http.client.HTTPSConnection, read_timeout)

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drops ``Authorization`` when the redirect leaves the host; refuses https -> http."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if old.scheme == "https" and new.scheme != "https":
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and old.netloc.lower() != new.netloc.lower():
            for store in (redirected.headers, redirected.unredirected_hdrs):
                for key in [k for k in store if k.lower() == "authorization"]:
                    del store[key]
        return redirected


@dataclass
class _CacheEntry:
    etag: str | None
    body: bytes
    link: str
    token_expiry: str
    fetched_at: float


def parse_token_expiry(value: str | None) -> datetime | None:
    """Parse ``github-authentication-token-expiration`` (``2026-12-18 12:00:00 UTC``)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_github_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def token_expiry_warning(status: TokenStatus, now: datetime, *, days: int = 14) -> str | None:
    """A warning if the token expires within ``days`` (or already has)."""
    if status.expires_at is None:
        return None
    remaining = status.expires_at - now
    if remaining <= timedelta(0):
        return "A GitHub-token lejárt."
    if remaining <= timedelta(days=days):
        return f"A GitHub-token {remaining.days + 1} napon belül lejár. Készíts újat."
    return None


class HttpGitHub(GitHub):
    def __init__(
        self,
        token: str,
        *,
        api_base: str = API_BASE,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
        cache_ttl: float = CACHE_TTL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not token or not _TOKEN_RE.match(token):
            raise GitHubAuthError(_MSG_AUTH)
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._api_host = urlsplit(self._api_base).netloc.lower()
        self._connect_timeout = connect_timeout
        self._max_attempts = max(1, max_attempts)
        self._cache_ttl = cache_ttl
        self._sleep = sleep
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._opener = urllib.request.build_opener(
            _HTTPHandler(read_timeout),
            _HTTPSHandler(read_timeout),
            _SafeRedirectHandler(),
        )

    def __repr__(self) -> str:
        return "HttpGitHub(token=***)"

    # ------------------------------------------------------------------ transport

    def _handle_http_error(self, exc: urllib.error.HTTPError) -> None:
        """Map an error status to a retry or a final exception (always raises)."""
        code = exc.code
        headers = exc.headers
        if code == 401:
            raise GitHubAuthError(_MSG_AUTH)
        if code in (403, 429):
            retry_after = _parse_retry_after(headers.get("Retry-After"))
            remaining = headers.get("X-RateLimit-Remaining")
            if retry_after is None and remaining == "0":
                reset = headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    retry_after = max(0.0, float(reset) - self._clock())
            if code == 429 or retry_after is not None or remaining == "0":
                error = GitHubRateLimited(
                    "A GitHub korlátozta a kéréseket (rate limit). Próbáld újra később.",
                    retry_after,
                )
                if retry_after is not None and retry_after <= MAX_RETRY_WAIT:
                    raise _Retry(error, retry_after)
                raise error
            raise GitHubAccessDenied(_MSG_DENIED)
        if code == 404:
            raise GitHubAccessDenied(_MSG_DENIED)
        if code >= 500:
            raise _Retry(GitHubUnavailable(_MSG_UNAVAILABLE))
        raise GitHubError(f"Váratlan GitHub-válasz (HTTP {code}).")

    def _attempt(
        self,
        url: str,
        accept: str,
        etag: str | None,
        consume: Callable[[Any], Any],
    ) -> tuple[int, Any, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self._connect_timeout)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 304:
                    return 304, exc.headers, None
                self._handle_http_error(exc)
            finally:
                exc.close()
            raise AssertionError("unreachable") from None  # pragma: no cover
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise _Retry(GitHubUnavailable(_MSG_UNAVAILABLE)) from exc
        with response:
            try:
                value = consume(response)
            except (OSError, http.client.HTTPException) as exc:
                raise _Retry(GitHubUnavailable(_MSG_UNAVAILABLE)) from exc
            return response.status, response.headers, value

    def _call(
        self,
        url: str,
        accept: str,
        consume: Callable[[Any], Any],
        etag: str | None = None,
    ) -> tuple[int, Any, Any]:
        """One logical request with retries (5xx, network errors, short Retry-After)."""
        last: GitHubError | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._attempt(url, accept, etag, consume)
            except _Retry as retry:
                last = retry.error
                if attempt + 1 >= self._max_attempts:
                    break
                delay = retry.delay if retry.delay is not None else BACKOFF_BASE * 2**attempt
                self._sleep(delay)
        assert last is not None
        raise last

    def _get_json(self, url: str) -> _CacheEntry:
        """GET with the 10-minute cache and ``ETag`` revalidation."""
        entry = self._cache.get(url)
        now = self._clock()
        if entry is not None and now - entry.fetched_at < self._cache_ttl:
            return entry

        def read(resp: Any) -> bytes:
            data: bytes = resp.read(MAX_JSON_BYTES + 1)
            if len(data) > MAX_JSON_BYTES:
                raise GitHubError("A GitHub-válasz váratlanul nagy.")
            return data

        status, headers, body = self._call(
            url,
            "application/vnd.github+json",
            read,
            etag=entry.etag if entry else None,
        )
        if status == 304 and entry is not None:
            entry.fetched_at = now
            return entry
        fresh = _CacheEntry(
            etag=headers.get("ETag"),
            body=body,
            link=headers.get("Link", "") or "",
            token_expiry=headers.get("github-authentication-token-expiration", "") or "",
            fetched_at=now,
        )
        self._cache[url] = fresh
        return fresh

    def _repo_url(self, repo: str) -> str:
        if not is_repo(repo):
            raise GitHubError(f"Érvénytelen repó: {repo!r}")
        return f"{self._api_base}/repos/{repo}"

    # ------------------------------------------------------------------ interface

    def check_access(self, repo: str) -> TokenStatus:
        # Always a fresh request: the point is to verify the token right now.
        url = self._repo_url(repo)
        self._cache.pop(url, None)
        entry = self._get_json(url)
        return TokenStatus(parse_token_expiry(entry.token_expiry))

    def list_releases(self, repo: str) -> list[Release]:
        url: str | None = f"{self._repo_url(repo)}/releases?per_page=100"
        releases: list[Release] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            entry = self._get_json(url)
            releases.extend(_parse_releases(entry.body))
            url = self._next_link(entry.link)
        else:
            raise GitHubError("Túl sok oldal a release-listában.")
        return _sort_releases(releases)

    def latest_stable(self, repo: str) -> Release | None:
        for release in self.list_releases(repo):
            if not release.prerelease and release.version is not None:
                return release
        return None

    def download_asset(
        self,
        repo: str,
        asset: Asset,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> None:
        url = f"{self._repo_url(repo)}/releases/assets/{int(asset.id)}"
        part = destination.with_name(destination.name + ".part")

        def save(resp: Any) -> int:
            declared = int(resp.headers.get("Content-Length") or 0)
            total = declared or asset.size
            done = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
                handle.flush()
                os.fsync(handle.fileno())
            if (declared and done != declared) or (asset.size and done != asset.size):
                raise http.client.IncompleteRead(b"", max(0, (declared or asset.size) - done))
            return done

        try:
            self._call(url, "application/octet-stream", save)
            os.replace(part, destination)
        finally:
            try:
                part.unlink()
            except FileNotFoundError:
                pass

    def _next_link(self, link_header: str) -> str | None:
        match = _NEXT_LINK_RE.search(link_header or "")
        if match is None:
            return None
        target = match.group(1)
        if urlsplit(target).netloc.lower() != self._api_host:
            raise GitHubError("A GitHub lapozó hivatkozása idegen hosztra mutat; megszakítva.")
        return target


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _parse_releases(body: bytes) -> list[Release]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise GitHubError("A GitHub válasza nem értelmezhető.") from exc
    if not isinstance(data, list):
        raise GitHubError("A GitHub válasza nem értelmezhető.")
    result: list[Release] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue  # drafts are never shown
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag:
            continue
        assets: list[Asset] = []
        for raw in item.get("assets") or []:
            if (
                isinstance(raw, dict)
                and isinstance(raw.get("id"), int)
                and isinstance(raw.get("name"), str)
            ):
                size = raw.get("size")
                assets.append(Asset(raw["id"], raw["name"], size if isinstance(size, int) else 0))
        result.append(
            Release(
                tag=tag,
                name=_text(item.get("name")) or tag,
                prerelease=bool(item.get("prerelease")),
                published_at=parse_github_time(item.get("published_at")),
                body=_text(item.get("body")),
                assets=tuple(assets),
            )
        )
    return result


def _sort_releases(releases: list[Release]) -> list[Release]:
    """Newest first: SemVer descending; tags that are not SemVer go last."""
    semver = [r for r in releases if r.version is not None]
    other = [r for r in releases if r.version is None]
    semver.sort(key=lambda r: r.version, reverse=True)  # type: ignore[arg-type, return-value]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    other.sort(key=lambda r: r.published_at or epoch, reverse=True)
    return semver + other
