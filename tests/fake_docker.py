"""An in-process fake of the Docker interface that records every call."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from installer.docker import ContainerInfo, Docker, DockerError, DockerInfo, ExecResult


@dataclass(frozen=True)
class ExecRecord:
    container: str
    args: tuple[str, ...]
    stdin: bytes | None = None
    stdout_to: Path | None = None
    stdin_from: Path | None = None

    @property
    def sql(self) -> str:
        return (self.stdin or b"").decode("utf-8", errors="replace")


class FakeDocker(Docker):
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.images: set[str] = set()
        self.containers: dict[str, ContainerInfo] = {}
        # What ``load_image`` makes available (the "contents" of the archive).
        self.archive_images: list[str] = []
        self.fail: set[str] = set()  # method names that raise DockerError
        self.free_root = "/var/lib/docker"
        self.logs = "app | starting..."
        self.exec_results: list[ExecResult] = []
        # Full record of every exec call, including what went to stdin.
        self.exec_log: list[ExecRecord] = []
        # Optional programmable fake: returns a result, or None to fall back to exec_results.
        self.exec_handler: Callable[[ExecRecord], ExecResult | None] | None = None
        # Called on every ``compose_up`` (project, compose_file, env_file): lets tests flip
        # the application's reported state.
        self.on_up: Callable[[str, Path, Path], None] | None = None
        self.up_log: list[tuple[str, Path, Path, str]] = []  # incl. APP_VERSION from env_file

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise DockerError(f"fake {name} hiba")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def info(self) -> DockerInfo:
        self._record("info")
        return DockerInfo("29.0.0", "v5.0.0")

    def root_dir(self) -> str | None:
        self._record("root_dir")
        return self.free_root

    def list_running_containers(self) -> list[ContainerInfo]:
        self._record("list_running_containers")
        return list(self.containers.values())

    def inspect_container(self, name: str) -> ContainerInfo:
        self._record("inspect_container", name)
        if name not in self.containers:
            raise DockerError(f"Nincs ilyen konténer: {name}")
        return self.containers[name]

    def image_exists(self, reference: str) -> bool:
        self._record("image_exists", reference)
        return reference in self.images

    def load_image(self, archive: Path) -> list[str]:
        self._record("load_image", archive)
        self.images.update(self.archive_images)
        return list(self.archive_images)

    def remove_image(self, reference: str) -> None:
        self._record("remove_image", reference)
        self.images.discard(reference)

    def compose_up(
        self, project: str, compose_file: Path, env_file: Path, *, force_recreate: bool = False
    ) -> None:
        self._record("compose_up", project, compose_file, env_file, force_recreate)
        version = ""
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_VERSION="):
                version = line.split("=", 1)[1].strip("'")
        self.up_log.append((project, compose_file, env_file, version))
        if self.on_up is not None:
            self.on_up(project, compose_file, env_file)

    def compose_stop(self, project: str, compose_file: Path, env_file: Path) -> None:
        self._record("compose_stop", project)

    def compose_ps(self, project: str, compose_file: Path, env_file: Path) -> str:
        self._record("compose_ps", project)
        return "app running"

    def compose_logs(
        self, project: str, compose_file: Path, env_file: Path, *, tail: int = 50
    ) -> str:
        self._record("compose_logs", project, tail)
        return self.logs

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
        self._record("exec", container, tuple(args))
        record = ExecRecord(container, tuple(args), stdin, stdout_to, stdin_from)
        self.exec_log.append(record)
        result = self.exec_handler(record) if self.exec_handler is not None else None
        if result is None:
            result = self.exec_results.pop(0) if self.exec_results else ExecResult(0)
        if stdout_to is not None and result.stdout:
            stdout_to.write_bytes(result.stdout)
        return result
