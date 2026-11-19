import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from installer.docker import DockerError
from installer.docker_cli import SubprocessDocker
from installer.health import HealthResponse, is_healthy
from installer.logging_ import Redactor
from installer.versions import Version


class Runner:
    """A stand-in for ``subprocess.run`` that records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: list[subprocess.CompletedProcess[bytes] | BaseException] = []

    def push(self, rc: int = 0, out: bytes = b"", err: bytes = b"") -> None:
        self.results.append(subprocess.CompletedProcess([], rc, out, err))

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"command": command, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class SubprocessDockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner()
        self.redactor = Redactor()
        self.docker = SubprocessDocker(redactor=self.redactor, runner=self.runner)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_every_call_uses_list_args_without_shell_and_with_timeout(self) -> None:
        self.runner.push(out=b"29.3.1\n")
        self.runner.push(out=b"v5.1.1\n")
        info = self.docker.info()
        self.assertEqual((info.version, info.compose_version), ("29.3.1", "v5.1.1"))
        for call in self.runner.calls:
            self.assertIsInstance(call["command"], list)
            self.assertIs(call["shell"], False)
            self.assertGreater(call["timeout"], 0)

    def test_old_compose_is_rejected(self) -> None:
        self.runner.push(out=b"24.0.0\n")
        self.runner.push(out=b"1.29.2\n")
        with self.assertRaises(DockerError) as ctx:
            self.docker.info()
        self.assertIn("v2", str(ctx.exception))

    def test_missing_docker_binary(self) -> None:
        self.runner.results.append(FileNotFoundError())
        with self.assertRaises(DockerError) as ctx:
            self.docker.info()
        self.assertIn("was not found", str(ctx.exception))

    def test_timeout_becomes_docker_error(self) -> None:
        self.runner.results.append(subprocess.TimeoutExpired("docker", 1))
        with self.assertRaises(DockerError):
            self.docker.image_exists("x:1")

    def test_errors_are_masked(self) -> None:
        self.redactor.register("topsecretvalue")
        self.runner.push(rc=1, err=b"denied for topsecretvalue")
        with self.assertRaises(DockerError) as ctx:
            self.docker.load_image(self.dir / "i.tar.gz")
        self.assertNotIn("topsecretvalue", str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_load_image_parses_references(self) -> None:
        self.runner.push(out=b"Loaded image: investment:0.2.0\nLoaded image: other:1\n")
        self.assertEqual(
            self.docker.load_image(self.dir / "i.tar.gz"), ["investment:0.2.0", "other:1"]
        )
        self.assertEqual(self.runner.calls[0]["command"][1:3], ["load", "-i"])

    def test_image_exists_uses_return_code(self) -> None:
        self.runner.push(rc=0)
        self.runner.push(rc=1)
        self.assertTrue(self.docker.image_exists("a:1"))
        self.assertFalse(self.docker.image_exists("b:1"))

    def test_compose_up_arguments_and_cwd(self) -> None:
        self.runner.push()
        compose = self.dir / "releases" / "0.2.0" / "docker-compose.deploy.yml"
        env = self.dir / "shared" / "compose.env"
        self.docker.compose_up("investment", compose, env, force_recreate=True)
        call = self.runner.calls[0]
        self.assertEqual(
            call["command"],
            [
                "docker", "compose", "-p", "investment", "-f", str(compose),
                "--env-file", str(env), "up", "-d", "--force-recreate",
            ],
        )  # fmt: skip
        self.assertEqual(call["cwd"], compose.parent)

    def test_inspect_parsing(self) -> None:
        payload = [
            {
                "Name": "/database-postgres-1",
                "Config": {
                    "Image": "postgres:17-alpine",
                    "Env": ["POSTGRES_USER=postgres", "PATH=/usr/bin"],
                    "ExposedPorts": {"5432/tcp": {}},
                },
                "NetworkSettings": {
                    "Networks": {
                        "shared-infra": {"Aliases": ["database-postgres-1", "postgres"]},
                        "other": {"Aliases": None},
                    }
                },
            }
        ]
        self.runner.push(out=b"database-postgres-1\n")
        self.runner.push(out=json.dumps(payload).encode())
        (container,) = self.docker.list_running_containers()
        self.assertEqual(container.name, "database-postgres-1")
        self.assertEqual(container.image, "postgres:17-alpine")
        self.assertEqual(container.env["POSTGRES_USER"], "postgres")
        self.assertEqual(container.exposed_ports, ("5432/tcp",))
        self.assertEqual(container.networks["shared-infra"], ("database-postgres-1", "postgres"))
        self.assertEqual(container.networks["other"], ())

    def test_no_running_containers(self) -> None:
        self.runner.push(out=b"")
        self.assertEqual(self.docker.list_running_containers(), [])
        self.assertEqual(len(self.runner.calls), 1)

    def test_exec_passes_stdin_as_input_never_as_argument(self) -> None:
        self.runner.push(out=b"ok")
        self.docker.exec("db", ["psql", "-U", "admin"], stdin=b"CREATE ROLE x PASSWORD 'pw';")
        call = self.runner.calls[0]
        self.assertEqual(call["command"], ["docker", "exec", "-i", "db", "psql", "-U", "admin"])
        self.assertEqual(call["input"], b"CREATE ROLE x PASSWORD 'pw';")
        self.assertNotIn("pw", " ".join(call["command"]))

    def test_exec_streams_to_and_from_files(self) -> None:
        source = self.dir / "in.dump"
        source.write_bytes(b"data")
        target = self.dir / "out.dump"
        self.runner.push()
        self.runner.push()
        self.docker.exec("db", ["pg_dump"], stdout_to=target)
        self.docker.exec("db", ["pg_restore"], stdin_from=source)
        self.assertTrue(target.exists())
        self.assertIsNone(self.runner.calls[1]["input"])
        self.assertIsNotNone(self.runner.calls[1]["stdin"])

    def test_exec_masks_stderr(self) -> None:
        self.redactor.register("dbpassword1")
        self.runner.push(rc=1, err=b"FATAL password dbpassword1 wrong")
        result = self.docker.exec("db", ["psql"])
        self.assertNotIn(b"dbpassword1", result.stderr)


class HealthTests(unittest.TestCase):
    V = Version.parse("0.2.0")

    def body(self, **data: object) -> HealthResponse:
        return HealthResponse(200, json.dumps(data).encode())

    def test_healthy(self) -> None:
        self.assertTrue(is_healthy(self.body(status="ok", version="0.2.0"), "version", self.V))
        self.assertTrue(is_healthy(self.body(version="v0.2.0"), "version", self.V))

    def test_unhealthy_cases(self) -> None:
        cases = [
            HealthResponse(503, b'{"version":"0.2.0"}'),
            self.body(version="0.1.0"),
            self.body(other="0.2.0"),
            self.body(version=2),
            HealthResponse(200, b"not json"),
            HealthResponse(200, b"[]"),
            self.body(version="garbage"),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.assertFalse(is_healthy(response, "version", self.V))

    def test_custom_version_field(self) -> None:
        self.assertTrue(is_healthy(self.body(build="0.2.0"), "build", self.V))


if __name__ == "__main__":
    unittest.main()
