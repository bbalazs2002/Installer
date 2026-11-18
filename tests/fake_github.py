"""A local HTTP server that imitates the parts of the GitHub API the installer uses.

Two servers are involved, like on the real GitHub: the API server and a separate
"storage" server that asset downloads are redirected to. The storage server rejects any
request that carries an ``Authorization`` header (as real object storage does).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

TOKEN = "github_pat_TESTTOKEN123456"


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class FakeGitHubState:
    token: str = TOKEN
    releases: list[dict[str, Any]] = field(default_factory=list)
    assets: dict[int, bytes] = field(default_factory=dict)
    per_page_cap: int = 100
    token_expiry: str | None = "2027-01-01 00:00:00 UTC"
    # Responses to inject before normal handling: (status, headers) per request.
    failures: deque[tuple[int, dict[str, str]]] = field(default_factory=deque)
    # Serve the asset body only partially (to test incomplete downloads), once.
    truncate_next_download: bool = False
    api_requests: list[Recorded] = field(default_factory=list)
    storage_requests: list[Recorded] = field(default_factory=list)
    etag_hits: int = 0
    storage_base: str = ""
    api_base: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


def make_release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    asset_ids: tuple[tuple[int, str, int], ...] = (),
    published: str = "2026-11-01T10:00:00Z",
    body: str = "",
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published,
        "body": body,
        "assets": [{"id": i, "name": n, "size": s} for i, n, s in asset_ids],
    }


class _ApiHandler(BaseHTTPRequestHandler):
    server_version = "FakeGitHub"
    state: FakeGitHubState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        with st.lock:
            st.api_requests.append(Recorded("GET", self.path, dict(self.headers.items())))
            if st.failures:
                status, headers = st.failures.popleft()
                self._send(status, b'{"message":"injected"}', headers)
                return
        if self.headers.get("Authorization") != f"Bearer {st.token}":
            self._send(401, b'{"message":"Bad credentials"}')
            return
        parts = urlsplit(self.path)
        segments = parts.path.strip("/").split("/")
        # /repos/{owner}/{repo}
        if len(segments) == 3 and segments[0] == "repos":
            headers = {"Content-Type": "application/json"}
            if st.token_expiry:
                headers["github-authentication-token-expiration"] = st.token_expiry
            self._send(
                200,
                b'{"full_name":"%s/%s"}' % (segments[1].encode(), segments[2].encode()),
                headers,
            )
            return
        # /repos/{owner}/{repo}/releases
        if len(segments) == 4 and segments[3] == "releases":
            self._releases(parts.query)
            return
        # /repos/{owner}/{repo}/releases/assets/{id}
        if len(segments) == 6 and segments[3] == "releases" and segments[4] == "assets":
            asset_id = int(segments[5])
            if asset_id not in st.assets:
                self._send(404, b'{"message":"Not Found"}')
                return
            self._send(302, b"", {"Location": f"{st.storage_base}/blob/{asset_id}"})
            return
        self._send(404, b'{"message":"Not Found"}')

    def _releases(self, query: str) -> None:
        st = self.state
        params = parse_qs(query)
        per_page = min(int(params.get("per_page", ["30"])[0]), st.per_page_cap)
        page = int(params.get("page", ["1"])[0])
        start = (page - 1) * per_page
        chunk = st.releases[start : start + per_page]
        body = json.dumps(chunk).encode()
        etag = f'"{hash(body) & 0xFFFFFFFF:x}"'
        headers = {"Content-Type": "application/json", "ETag": etag}
        if start + per_page < len(st.releases):
            headers["Link"] = (
                f"<{st.api_base}/repos/o/r/releases?per_page={per_page}&page={page + 1}>"
                '; rel="next"'
            )
        if self.headers.get("If-None-Match") == etag:
            with st.lock:
                st.etag_hits += 1
            self._send(304, b"", headers)
            return
        self._send(200, body, headers)


class _StorageHandler(BaseHTTPRequestHandler):
    state: FakeGitHubState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        with st.lock:
            st.storage_requests.append(Recorded("GET", self.path, dict(self.headers.items())))
        if "Authorization" in self.headers:
            # Real object storage refuses a request that carries foreign credentials.
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        asset_id = int(self.path.rsplit("/", 1)[1])
        data = st.assets[asset_id]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if st.truncate_next_download:
            st.truncate_next_download = False
            self.wfile.write(data[: len(data) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(data)


class FakeGitHub:
    """Context manager running both servers on ephemeral localhost ports."""

    def __init__(self) -> None:
        self.state = FakeGitHubState()
        api_handler = type("ApiHandler", (_ApiHandler,), {"state": self.state})
        storage_handler = type("StorageHandler", (_StorageHandler,), {"state": self.state})
        self._api = ThreadingHTTPServer(("127.0.0.1", 0), api_handler)
        self._storage = ThreadingHTTPServer(("127.0.0.1", 0), storage_handler)
        self.state.api_base = f"http://127.0.0.1:{self._api.server_address[1]}"
        self.state.storage_base = f"http://127.0.0.1:{self._storage.server_address[1]}"
        self._threads = [
            threading.Thread(
                target=self._api.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            ),
            threading.Thread(
                target=self._storage.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            ),
        ]

    @property
    def api_base(self) -> str:
        return self.state.api_base

    def __enter__(self) -> FakeGitHub:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for server in (self._api, self._storage):
            server.shutdown()
            server.server_close()
