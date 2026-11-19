"""In-memory release data and a GitHub fake for installer tests."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telepito.github import Asset, GitHub, ProgressCallback, Release, TokenStatus

COMPOSE = """services:
  app:
    image: investment:${APP_VERSION}
    env_file: .env.production
    ports:
      - "127.0.0.1:${PORT}:${PORT}"
    networks: [db]
    restart: unless-stopped
networks:
  db:
    external: true
    name: "${DB_NETWORK}"
"""

MANIFEST: dict[str, Any] = {
    "schemaVersion": 1,
    "name": "investment",
    "displayName": "Investment",
    "image": {"name": "investment"},
    "compose": {"file": "docker-compose.deploy.yml", "projectName": "investment"},
    "port": {"default": 2570, "env": "PORT"},
    "health": {"path": "/healthz", "timeoutSeconds": 10, "versionField": "version"},
    "database": {
        "required": True,
        "description": "Postgres kell.",
        "urlEnv": "DATABASE_URL",
        "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
    },
    "migrations": {"listAsset": "migrations.txt"},
    "env": [
        {"name": "NODE_ENV", "fixed": "production"},
        {"name": "BASE_PATH", "default": "/investment/"},
        {
            "name": "APIKEY_MASTER_SECRET",
            "secret": True,
            "required": True,
            "generate": {"bytes": 48},
            "minLength": 32,
        },
    ],
    "proxy": {"snippet": 'ProxyPass "/investment/" "http://127.0.0.1:{port}/"'},
    "postInstall": ["Hozz létre felhasználót"],
    "keep": 2,
}


@dataclass
class ReleaseSpec:
    version: str
    manifest: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(MANIFEST))
    compose: str = COMPOSE
    migrations: Sequence[str] = ("m1",)
    image: bytes = b"fake-image-bytes" * 1000
    prerelease: bool = False

    def files(self) -> dict[str, bytes]:
        files = {
            "deploy.json": json.dumps(self.manifest).encode(),
            "docker-compose.deploy.yml": self.compose.encode(),
            "image.tar.gz": self.image,
            "migrations.txt": ("\n".join(self.migrations) + "\n").encode(),
        }
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
        ).encode()
        return files


class MemoryGitHub(GitHub):
    """Serves releases built from :class:`ReleaseSpec` objects."""

    def __init__(self) -> None:
        self._files: dict[int, bytes] = {}
        self._releases: list[Release] = []
        self._next_id = 1
        self.downloads: list[str] = []

    def add(
        self, spec: ReleaseSpec, *, tamper: str | None = None, omit: Sequence[str] = ()
    ) -> Release:
        files = spec.files()
        if tamper is not None:
            files[tamper] = files[tamper] + b"X"  # contents no longer match SHA256SUMS
        assets: list[Asset] = []
        for name, data in files.items():
            if name in omit:
                continue
            asset = Asset(self._next_id, name, len(data))
            self._files[asset.id] = data
            assets.append(asset)
            self._next_id += 1
        release = Release(
            tag=f"v{spec.version}",
            name=spec.version,
            prerelease=spec.prerelease,
            published_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            body="",
            assets=tuple(assets),
        )
        self._releases.append(release)
        return release

    def add_raw(self, tag: str, files: dict[str, bytes], *, prerelease: bool = False) -> Release:
        """A release made of arbitrary files (used for the installer's own releases)."""
        assets = []
        for name, data in files.items():
            asset = Asset(self._next_id, name, len(data))
            self._files[asset.id] = data
            assets.append(asset)
            self._next_id += 1
        release = Release(
            tag=tag,
            name=tag,
            prerelease=prerelease,
            published_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            body="",
            assets=tuple(assets),
        )
        self._releases.append(release)
        return release

    def check_access(self, repo: str) -> TokenStatus:
        return TokenStatus(None)

    def list_releases(self, repo: str) -> list[Release]:
        """Newest first, like the real client."""
        return sorted(self._releases, key=lambda r: r.version, reverse=True)  # type: ignore[arg-type, return-value]

    def latest_stable(self, repo: str) -> Release | None:
        stable = [r for r in self.list_releases(repo) if not r.prerelease]
        return stable[0] if stable else None

    def download_asset(
        self,
        repo: str,
        asset: Asset,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.downloads.append(asset.name)
        data = self._files[asset.id]
        destination.write_bytes(data)
        if progress is not None:
            progress(len(data), len(data))
