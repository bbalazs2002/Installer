"""The state of the installed applications (menu 6, ``installer status``).

Reads ``installs.json`` and asks Docker and the health endpoint; it never writes anything and
never touches a secret.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from .docker import Docker, DockerError
from .health import HealthChecker
from .installer import MANIFEST_ASSET, Layout
from .manifest import ManifestError, load_manifest
from .state import InstallRecord, StateError, load_install_info
from .versions import Version


@dataclass(frozen=True)
class InstallStatus:
    name: str
    repo: str
    path: str
    version: str
    port: int | None
    health: str  # "ok", "not responding", or a short problem description
    containers: str
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def collect_status(
    records: Sequence[InstallRecord], docker: Docker, health: HealthChecker
) -> list[InstallStatus]:
    return [_one(record, docker, health) for record in records]


def _one(record: InstallRecord, docker: Docker, health: HealthChecker) -> InstallStatus:
    from pathlib import Path

    target = Path(record.path)
    base = {"name": record.name, "repo": record.repo, "path": record.path}
    try:
        info = load_install_info(target)
    except StateError as exc:
        return InstallStatus(**base, version=record.version, port=None,
                             health="unknown", containers="", note=str(exc))  # fmt: skip
    if info is None:
        return InstallStatus(**base, version=record.version, port=None, health="unknown",
                             containers="", note="There is no installation in the target directory.")  # fmt: skip

    layout = Layout(target)
    release_dir = layout.release_dir(info.version)
    try:
        manifest = load_manifest((release_dir / MANIFEST_ASSET).read_bytes())
    except (OSError, ManifestError) as exc:
        note = f"The release descriptor cannot be read: {exc}"
        return InstallStatus(
            **base,
            version=info.version,
            port=info.port,
            health="unknown",
            containers="",
            note=note,
        )

    containers = ""
    note = ""
    try:
        containers = docker.compose_ps(
            manifest.project_name, release_dir / manifest.compose_file, layout.compose_env
        ).strip()
    except DockerError as exc:
        note = str(exc)

    port = info.port or manifest.port_default
    url = f"http://127.0.0.1:{port}{manifest.health_path}"
    status = "not responding"
    try:
        response = health(url)
        parsed = _reported_version(response.body, manifest.health_version_field)
        expected = Version.try_parse(info.version)
        if response.status == 200 and parsed is not None and parsed == expected:
            status = "ok"
        elif response.status == 200:
            status = f"reports a different version ({parsed or '?'})"
        else:
            status = f"HTTP {response.status}"
    except OSError:
        pass
    return InstallStatus(**base, version=info.version, port=port, health=status,
                         containers=containers, note=note)  # fmt: skip


def _reported_version(body: bytes, field: str) -> Version | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    value = data.get(field) if isinstance(data, dict) else None
    return Version.try_parse(value) if isinstance(value, str) else None


def format_status(items: Sequence[InstallStatus]) -> str:
    if not items:
        return "There are no installed applications yet."
    blocks: list[str] = []
    for item in items:
        lines = [
            f"{item.name}  {item.version}  ({item.repo})",
            f"  folder:  {item.path}",
            f"  status: {item.health}" + (f"  (port {item.port})" if item.port else ""),
        ]
        if item.containers:
            lines.append("  containers:")
            lines += [f"    {line}" for line in item.containers.splitlines()]
        if item.note:
            lines.append(f"  note: {item.note}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
