"""The real :class:`~installer.docker.Docker`, driving the ``docker`` command line.

Rules (specification 9): list arguments only (``shell=False``), a timeout on every call,
secrets only through stdin or files (never arguments or environment), and all output that
may reach a message or the log is masked first.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .docker import ContainerInfo, Docker, DockerError, DockerInfo, ExecResult, HelperMount
from .logging_ import Redactor

DEFAULT_TIMEOUT = 120.0
LOAD_TIMEOUT = 1800.0  # loading a few hundred MB image
_LOADED_RE = re.compile(r"^Loaded image:\s*(\S+)\s*$", re.MULTILINE)
_COMPOSE_MAJOR_RE = re.compile(r"v?(\d+)\.")


class SubprocessDocker(Docker):
    def __init__(
        self,
        *,
        executable: str = "docker",
        redactor: Redactor | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self._exe = executable
        self._redactor = redactor or Redactor()
        self._runner = runner

    # ------------------------------------------------------------------ plumbing

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        stdin_file: Path | None = None,
        stdout_file: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: Path | None = None,
    ) -> ExecResult:
        command = [self._exe, *args]
        stdin_handle = open(stdin_file, "rb") if stdin_file is not None else None  # noqa: SIM115
        stdout_handle = open(stdout_file, "wb") if stdout_file is not None else None  # noqa: SIM115
        try:
            try:
                completed = self._runner(
                    command,
                    input=stdin if stdin_handle is None else None,
                    stdin=stdin_handle if stdin_handle is not None else None,
                    stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    cwd=cwd,
                    shell=False,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise DockerError("The docker command was not found.") from exc
            except subprocess.TimeoutExpired as exc:
                raise DockerError(
                    f"The docker command was aborted because of a timeout ({args[0]})."
                ) from exc
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
            if stdout_handle is not None:
                stdout_handle.close()
        return ExecResult(completed.returncode, completed.stdout or b"", completed.stderr or b"")

    def _check(self, result: ExecResult, what: str) -> ExecResult:
        if result.returncode != 0:
            raise DockerError(f"{what} failed: {self._tail(result.stderr)}")
        return result

    def _tail(self, data: bytes, lines: int = 5) -> str:
        text = data.decode("utf-8", errors="replace").strip()
        tail = "\n".join(text.splitlines()[-lines:]) or "(no error message)"
        return self._redactor.redact(tail)

    def _compose_args(self, project: str, compose_file: Path, env_file: Path) -> list[str]:
        return [
            "compose",
            "-p",
            project,
            "-f",
            str(compose_file),
            "--env-file",
            str(env_file),
        ]

    # ------------------------------------------------------------------ interface

    def info(self) -> DockerInfo:
        server = self._check(
            self._run(["version", "--format", "{{.Server.Version}}"]), "Reaching Docker"
        )
        compose = self._check(
            self._run(["compose", "version", "--short"]), "Checking docker compose"
        )
        compose_version = compose.stdout.decode().strip()
        match = _COMPOSE_MAJOR_RE.match(compose_version + ".")
        if match is None or int(match.group(1)) < 2:
            raise DockerError(
                f"docker compose v2 or newer is required (found: {compose_version or '?'})."
            )
        return DockerInfo(server.stdout.decode().strip(), compose_version)

    def root_dir(self) -> str | None:
        result = self._run(["info", "--format", "{{.DockerRootDir}}"])
        text = result.stdout.decode().strip()
        return text if result.returncode == 0 and text else None

    def list_running_containers(self) -> list[ContainerInfo]:
        names = (
            self._check(
                self._run(["ps", "--format", "{{.Names}}"]), "Listing the running containers"
            )
            .stdout.decode()
            .split()
        )
        if not names:
            return []
        return self._inspect(names)

    def inspect_container(self, name: str) -> ContainerInfo:
        return self._inspect([name])[0]

    def _inspect(self, names: Sequence[str]) -> list[ContainerInfo]:
        result = self._check(self._run(["inspect", *names]), "Inspecting the container")
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise DockerError("The output of docker inspect cannot be understood.") from exc
        return [_parse_inspect(item) for item in data]

    def image_exists(self, reference: str) -> bool:
        return self._run(["image", "inspect", reference]).returncode == 0

    def load_image(self, archive: Path) -> list[str]:
        result = self._check(
            self._run(["load", "-i", str(archive)], timeout=LOAD_TIMEOUT), "Loading the image"
        )
        return _LOADED_RE.findall(result.stdout.decode("utf-8", errors="replace"))

    def remove_image(self, reference: str) -> None:
        self._check(self._run(["image", "rm", reference]), "Removing the image")

    def compose_up(
        self, project: str, compose_file: Path, env_file: Path, *, force_recreate: bool = False
    ) -> None:
        args = [*self._compose_args(project, compose_file, env_file), "up", "-d"]
        if force_recreate:
            args.append("--force-recreate")
        # The project directory (relative env_file, etc.) is the compose file's directory.
        self._check(self._run(args, timeout=600, cwd=compose_file.parent), "Starting")

    def compose_stop(self, project: str, compose_file: Path, env_file: Path) -> None:
        args = [*self._compose_args(project, compose_file, env_file), "stop"]
        self._check(self._run(args, timeout=300, cwd=compose_file.parent), "Stopping")

    def compose_ps(self, project: str, compose_file: Path, env_file: Path) -> str:
        args = [*self._compose_args(project, compose_file, env_file), "ps"]
        result = self._check(self._run(args, cwd=compose_file.parent), "Querying the status")
        return self._redactor.redact(result.stdout.decode("utf-8", errors="replace"))

    def compose_logs(
        self, project: str, compose_file: Path, env_file: Path, *, tail: int = 50
    ) -> str:
        args = [
            *self._compose_args(project, compose_file, env_file),
            "logs",
            "--no-color",
            "--tail",
            str(tail),
        ]
        result = self._run(args, cwd=compose_file.parent)
        text = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        return self._redactor.redact(text)

    def compose_config(self, project: str, compose_file: Path, env_file: Path) -> dict[str, Any]:
        args = [*self._compose_args(project, compose_file, env_file), "config", "--format", "json"]
        result = self._check(
            self._run(args, cwd=compose_file.parent), "Reading the compose configuration"
        )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise DockerError("The output of docker compose config cannot be understood.") from exc
        if not isinstance(data, dict):
            raise DockerError("The output of docker compose config cannot be understood.")
        return data

    def volume_list(self, project: str) -> list[str]:
        result = self._check(
            self._run(
                [
                    "volume",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--format",
                    "{{.Name}}",
                ]
            ),
            "Listing the volumes",
        )
        return result.stdout.decode("utf-8", errors="replace").split()

    def run_helper(
        self,
        image: str,
        command: Sequence[str],
        *,
        mounts: Sequence[HelperMount] = (),
        stdin_from: Path | None = None,
        stdout_to: Path | None = None,
        timeout: float = 3600,
    ) -> ExecResult:
        if not command:
            raise DockerError("A helper container needs a command.")
        args = ["run", "--rm", "-i", "--pull", "never", "--network", "none", "--user", "0"]
        for mount in mounts:
            if ":" in mount.source or "," in mount.source:
                raise DockerError(f"The path {mount.source!r} cannot be mounted.")
            args += ["-v", f"{mount.source}:{mount.target}" + (":ro" if mount.read_only else "")]
        args += ["--entrypoint", command[0], image, *command[1:]]
        result = self._run(args, stdin_file=stdin_from, stdout_file=stdout_to, timeout=timeout)
        masked = self._redactor.redact(result.stderr.decode("utf-8", errors="replace"))
        return ExecResult(result.returncode, result.stdout, masked.encode("utf-8"))

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
        command = ["exec", "-i", container, *args]
        result = self._run(
            command, stdin=stdin, stdin_file=stdin_from, stdout_file=stdout_to, timeout=timeout
        )
        # Whatever comes back may be shown or logged: mask it here, at the single exit.
        masked = self._redactor.redact(result.stderr.decode("utf-8", errors="replace"))
        return ExecResult(result.returncode, result.stdout, masked.encode("utf-8"))


def _parse_inspect(item: dict[str, Any]) -> ContainerInfo:
    config = item.get("Config") or {}
    env: dict[str, str] = {}
    for entry in config.get("Env") or []:
        key, _, value = str(entry).partition("=")
        env[key] = value
    networks: dict[str, tuple[str, ...]] = {}
    settings = item.get("NetworkSettings") or {}
    for name, details in (settings.get("Networks") or {}).items():
        networks[name] = tuple((details or {}).get("Aliases") or ())
    return ContainerInfo(
        name=str(item.get("Name", "")).lstrip("/"),
        image=str(config.get("Image", "")),
        env=env,
        exposed_ports=tuple((config.get("ExposedPorts") or {}).keys()),
        networks=networks,
    )
