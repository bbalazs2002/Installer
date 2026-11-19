"""The state of the installed applications (menu 6, ``installer status``).

Reads ``installs.json`` and asks Docker and the health endpoint; it never writes anything and
never touches a secret.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .backup import list_volume_backups
from .docker import Docker, DockerError
from .health import HealthChecker
from .installer import MANIFEST_ASSET, Layout
from .manifest import Manifest, ManifestError, load_manifest
from .state import InstallRecord, StateError, load_install_info
from .versions import Version
from .volumebackup import measure_volume
from .volumes import ResolvedVolume, check_volumes, parse_compose_config


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
    # One entry per data volume: name, kind, mountPath, backup, sizeBytes, lastBackup.
    volumes: tuple[dict[str, object], ...] = ()

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def collect_status(
    records: Sequence[InstallRecord], docker: Docker, health: HealthChecker
) -> list[InstallStatus]:
    return [_one(record, docker, health) for record in records]


def _one(record: InstallRecord, docker: Docker, health: HealthChecker) -> InstallStatus:
    from pathlib import Path

    target = Path(record.path)
    base: dict[str, Any] = {"name": record.name, "repo": record.repo, "path": record.path}
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
    volumes = _volume_rows(docker, manifest, layout, info.version)
    return InstallStatus(**base, version=info.version, port=port, health=status,
                         containers=containers, note=note, volumes=volumes)  # fmt: skip


def _volume_rows(
    docker: Docker, manifest: Manifest, layout: Layout, version: str
) -> tuple[dict[str, object], ...]:
    """The data volumes with their size and the time of the newest archive of each."""
    if not manifest.volumes:
        return ()
    resolved: dict[str, ResolvedVolume] = {}
    try:
        config = docker.compose_config(
            manifest.project_name,
            layout.release_dir(version) / manifest.compose_file,
            layout.compose_env,
        )
        check = check_volumes(
            parse_compose_config(config),
            manifest.volumes,
            project=manifest.project_name,
            data_dir=str(layout.data_dir),
        )
        resolved = {v.name: v for v in check.resolved}
    except (DockerError, ValueError):
        pass
    image = f"{manifest.image_name}:{Version.parse(version).image_tag}"
    newest: dict[str, str] = {}
    for item in list_volume_backups(layout.backups):  # newest first
        newest.setdefault(item.database, item.created)
    rows: list[dict[str, object]] = []
    for spec in manifest.volumes:
        volume = resolved.get(spec.name)
        rows.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "mountPath": spec.mount_path,
                "backup": spec.backup,
                "sizeBytes": measure_volume(docker, volume, image) if volume else None,
                "lastBackup": newest.get(spec.name),
            }
        )
    return tuple(rows)


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
        if item.volumes:
            lines.append("  data volumes:")
            for row in item.volumes:
                size = row["sizeBytes"]
                size_text = (
                    f"{int(size) / 2**20:.1f} MB" if isinstance(size, int) else "size unknown"
                )
                last = row["lastBackup"] or ("never" if row["backup"] else "not backed up")
                lines.append(
                    f"    {row['name']} ({row['kind']}) {row['mountPath']}  {size_text}, last backup: {last}"
                )
        if item.note:
            lines.append(f"  note: {item.note}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
