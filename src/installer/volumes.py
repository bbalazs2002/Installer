"""Data volumes: reading the normalised compose model and checking it against ``deploy.json``.

Pure logic. The input is the JSON that ``docker compose config --format json`` prints, so
variables are already substituted and the short and long volume syntaxes are already unified.

The contract (docs/release-contract.md) allows exactly two kinds of persistent data:

* a **named volume** of the compose project (``<project>_<name>``), and
* a **bind mount** whose source is ``${DATA_DIR}/<hostPath>``, a directory the installer creates.

Everything else that touches the host (``docker.sock``, other bind mounts, ``external`` volumes,
renamed volumes) is a blocker, and every volume must be declared in ``deploy.json`` (and the
other way round) so that it is backed up and shown in the plan.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any

from .manifest import VolumeSpec

DATA_DIR_ENV = "DATA_DIR"
UID_ENV = "APP_UID"
GID_ENV = "APP_GID"
DOCKER_SOCKET_NAMES = ("docker.sock",)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class Mount:
    """One mount of one service, as Compose resolved it."""

    service: str
    type: str  # volume | bind | tmpfs | ...
    source: str  # volume key (type volume), absolute host path (type bind) or ""
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class ComposeVolumeModel:
    mounts: tuple[Mount, ...]
    # top-level ``volumes:`` key -> the real Docker volume name Compose will use
    volume_names: dict[str, str]
    external: frozenset[str]  # keys declared ``external: true``


@dataclass(frozen=True)
class ResolvedVolume:
    """A declared volume together with where its data really is."""

    spec: VolumeSpec
    source: str  # the Docker volume name, or the absolute host directory of a bind mount

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_bind(self) -> bool:
        return self.spec.is_bind


@dataclass(frozen=True)
class VolumeCheck:
    resolved: tuple[ResolvedVolume, ...]
    problems: tuple[str, ...]  # block the installation
    anonymous: tuple[Mount, ...]  # volumes without a name: never backed up, only reported

    @property
    def ok(self) -> bool:
        return not self.problems


class ComposeConfigError(ValueError):
    """The ``compose config`` output has an unexpected shape."""


def parse_compose_config(data: Any) -> ComposeVolumeModel:
    """Extract the mounts and the top-level volumes from ``compose config`` JSON."""
    if not isinstance(data, dict):
        raise ComposeConfigError("the compose configuration is not an object")
    services = data.get("services") or {}
    if not isinstance(services, dict):
        raise ComposeConfigError("the compose services are not an object")
    mounts: list[Mount] = []
    for service, body in services.items():
        if not isinstance(body, dict):
            continue
        for item in body.get("volumes") or []:
            if not isinstance(item, dict):
                raise ComposeConfigError(f"service {service}: a volume entry is not an object")
            mounts.append(
                Mount(
                    service=str(service),
                    type=str(item.get("type", "")),
                    source=str(item.get("source") or ""),
                    target=str(item.get("target") or ""),
                    read_only=bool(item.get("read_only", False)),
                )
            )
    names: dict[str, str] = {}
    external: set[str] = set()
    top = data.get("volumes") or {}
    if not isinstance(top, dict):
        raise ComposeConfigError("the top-level volumes are not an object")
    for key, body in top.items():
        body = body if isinstance(body, dict) else {}
        names[str(key)] = str(body.get("name") or key)
        if body.get("external"):
            external.add(str(key))
    return ComposeVolumeModel(tuple(mounts), names, frozenset(external))


def bind_source(spec: VolumeSpec, data_dir: str) -> str:
    """The host directory a ``bind`` volume must use: ``<data_dir>/<hostPath>``."""
    assert spec.host_path is not None
    return posixpath.join(data_dir.replace("\\", "/"), spec.host_path)


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def check_volumes(
    model: ComposeVolumeModel,
    declared: tuple[VolumeSpec, ...],
    *,
    project: str,
    data_dir: str,
) -> VolumeCheck:
    """Compare what the compose file mounts with what ``deploy.json`` declares."""
    problems: list[str] = []
    anonymous: list[Mount] = []
    named = {v.name: v for v in declared if not v.is_bind}
    binds = {_norm(bind_source(v, data_dir)): v for v in declared if v.is_bind}
    found: dict[str, ResolvedVolume] = {}

    for mount in model.mounts:
        if mount.type == "volume":
            _check_named(mount, model, named, project, problems, found, anonymous)
        elif mount.type == "bind":
            _check_bind(mount, binds, data_dir, problems, found)
        # tmpfs and other kinds hold no persistent data

    for spec in declared:
        resolved = found.get(spec.name)
        if resolved is None:
            problems.append(
                f"deploy.json declares the volume {spec.name!r}, but the compose file "
                "does not mount it."
            )
        elif not any(
            m.target.rstrip("/") == spec.mount_path.rstrip("/")
            for m in model.mounts
            if _mount_is(m, resolved)
        ):
            problems.append(
                f"The volume {spec.name!r} is mounted at a different path than the "
                f"mountPath in deploy.json ({spec.mount_path})."
            )
    order = {v.name: i for i, v in enumerate(declared)}
    resolved_all = tuple(sorted(found.values(), key=lambda r: order[r.name]))
    return VolumeCheck(resolved_all, tuple(dict.fromkeys(problems)), tuple(anonymous))


def _mount_is(mount: Mount, resolved: ResolvedVolume) -> bool:
    if resolved.is_bind:
        return mount.type == "bind" and _norm(mount.source) == _norm(resolved.source)
    return mount.type == "volume" and mount.source == resolved.spec.name


def _check_named(
    mount: Mount,
    model: ComposeVolumeModel,
    named: dict[str, VolumeSpec],
    project: str,
    problems: list[str],
    found: dict[str, ResolvedVolume],
    anonymous: list[Mount],
) -> None:
    if not mount.source:
        anonymous.append(mount)
        return
    key = mount.source
    if key in model.external:
        problems.append(
            f"The volume {key!r} is external (not owned by this project); that is not allowed."
        )
        return
    real = model.volume_names.get(key, f"{project}_{key}")
    if real != f"{project}_{key}":
        problems.append(
            f"The volume {key!r} has the custom name {real!r}; it must be {project}_{key} "
            "(do not set name: on a volume)."
        )
        return
    spec = named.get(key)
    if spec is None:
        problems.append(
            f"The compose file mounts the volume {key!r}, which deploy.json does not declare."
        )
        return
    found[key] = ResolvedVolume(spec, real)


def _check_bind(
    mount: Mount,
    binds: dict[str, VolumeSpec],
    data_dir: str,
    problems: list[str],
    found: dict[str, ResolvedVolume],
) -> None:
    source = _norm(mount.source)
    if any(source.endswith("/" + name) or source == name for name in DOCKER_SOCKET_NAMES):
        problems.append(
            f"Mounting the Docker socket ({mount.source}) gives the container control of the "
            "server; that is not allowed."
        )
        return
    spec = binds.get(source)
    if spec is None:
        if source.startswith(_norm(data_dir) + "/"):
            problems.append(
                f"The compose file mounts {mount.source!r} below the data directory, "
                "which deploy.json does not declare."
            )
        elif _WINDOWS_DRIVE_RE.match(mount.source) or not source.startswith("/"):
            problems.append(
                f"The bind mount {mount.source!r} is relative or not a plain path; use "
                f"${{{DATA_DIR_ENV}}}/<directory> and declare it in deploy.json."
            )
        else:
            problems.append(
                f"The bind mount {mount.source!r} is outside the data directory "
                f"({data_dir}); only ${{{DATA_DIR_ENV}}}/<directory> is allowed."
            )
        return
    found[spec.name] = ResolvedVolume(spec, source)


def describe(volume: VolumeSpec) -> str:
    """One line for the plan and the status screen."""
    kind = "bind mount" if volume.is_bind else "volume"
    backup = "backed up" if volume.backup else "NOT backed up"
    extra = f" - {volume.description}" if volume.description else ""
    return f"{volume.name} ({kind}) -> {volume.mount_path}, {backup}{extra}"
