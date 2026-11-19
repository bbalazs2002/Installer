"""The single place for Docker calls: an interface plus the data it exchanges.

The real implementation (``docker_cli.SubprocessDocker``) runs every command with list arguments
(``shell=False``) and a timeout, and passes secrets only through stdin or files, never through
arguments or the environment. Tests use an in-process fake (``tests/fake_docker.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import TelepitoError


class DockerError(TelepitoError):
    """A Docker call failed or Docker is unavailable. The message is user-facing."""


@dataclass(frozen=True)
class DockerInfo:
    version: str
    compose_version: str


@dataclass(frozen=True)
class ContainerInfo:
    """What the installer needs to know about a (running) container."""

    name: str
    image: str
    env: Mapping[str, str] = field(default_factory=dict)
    exposed_ports: tuple[str, ...] = ()
    # network name -> aliases of the container on that network
    networks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Docker(ABC):
    @abstractmethod
    def info(self) -> DockerInfo:
        """Docker and Compose versions; raises :class:`DockerError` if either is unusable."""

    @abstractmethod
    def list_running_containers(self) -> list[ContainerInfo]:
        """Running containers, fully inspected."""

    @abstractmethod
    def inspect_container(self, name: str) -> ContainerInfo:
        """One container by name; raises :class:`DockerError` if it does not exist."""

    @abstractmethod
    def root_dir(self) -> str | None:
        """Docker's data directory (for the free-space check), if it can be determined."""

    @abstractmethod
    def image_exists(self, reference: str) -> bool: ...

    @abstractmethod
    def load_image(self, archive: Path) -> list[str]:
        """``docker load -i``; returns the loaded ``name:tag`` references."""

    @abstractmethod
    def remove_image(self, reference: str) -> None: ...

    @abstractmethod
    def compose_up(
        self,
        project: str,
        compose_file: Path,
        env_file: Path,
        *,
        force_recreate: bool = False,
    ) -> None:
        """``docker compose -p ... -f ... --env-file ... up -d``."""

    @abstractmethod
    def compose_stop(self, project: str, compose_file: Path, env_file: Path) -> None: ...

    @abstractmethod
    def compose_ps(self, project: str, compose_file: Path, env_file: Path) -> str: ...

    @abstractmethod
    def compose_logs(
        self, project: str, compose_file: Path, env_file: Path, *, tail: int = 50
    ) -> str: ...

    @abstractmethod
    def exec(
        self,
        container: str,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        stdout_to: Path | None = None,
        stdin_from: Path | None = None,
        timeout: float = 300,
    ) -> ExecResult:
        """``docker exec -i``. Secrets go through ``stdin`` / ``stdin_from``, never ``args``.

        ``stdout_to`` streams the output into a file (e.g. ``pg_dump``), ``stdin_from``
        streams a file in (e.g. ``pg_restore``).
        """
