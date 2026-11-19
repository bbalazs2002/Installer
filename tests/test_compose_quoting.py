"""Real ``docker compose config`` check of the env file quoting (specification 6.7).

Marked as a Docker test: skipped when ``docker compose`` is unavailable or when the
environment variable ``TELEPITO_SKIP_DOCKER_TESTS`` is set. It needs no running daemon.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from installer.envfile import update_env_file

TRICKY = {
    "WITH_DOLLAR": "p$ss",
    "BRACES": "${NOT_EXPANDED}",
    "DOLLAR_PAIR": "dollar$$two",
    "HASH": "abc#def # trailing",
    "SPACES": "two  words and more",
    "EQUALS": "a=b=c",
    "BACKSLASH": "back\\slash\\n",
    "DOUBLE_QUOTE": 'say "hi"',
    "LEADING_TRAILING": "  padded  ",
    "UNICODE": "árvíztűrő tükörfúrógép",
    "PERCENT": "100%",
    "BACKTICK": "`cmd` $(cmd)",
    "EMPTY": "",
}


def _docker_available() -> bool:
    if os.environ.get("TELEPITO_SKIP_DOCKER_TESTS") or shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version", "--short"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@unittest.skipUnless(_docker_available(), "docker compose is not available")
class ComposeQuotingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "docker-compose.deploy.yml").write_text(
            "services:\n"
            "  app:\n"
            "    image: busybox:${APP_VERSION}\n"
            "    env_file: .env.production\n"
            "    ports:\n"
            '      - "127.0.0.1:${PORT}:${PORT}"\n',
            encoding="utf-8",
        )

    def config(self) -> dict[str, object]:
        result = subprocess.run(
            [
                "docker", "compose",
                "-f", str(self.dir / "docker-compose.deploy.yml"),
                "--env-file", str(self.dir / "compose.env"),
                "config", "--format", "json",
            ],
            capture_output=True,
            timeout=60,
            cwd=self.dir,
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        data: dict[str, object] = json.loads(result.stdout)
        return data

    def test_values_survive_compose_unchanged(self) -> None:
        update_env_file(self.dir / ".env.production", TRICKY)
        update_env_file(self.dir / "compose.env", {"APP_VERSION": "0.1.0", "PORT": "2570"})
        service = self.config()["services"]["app"]  # type: ignore[index]
        environment = service["environment"]
        for key, original in TRICKY.items():
            # `docker compose config` prints every literal `$` as `$$` so that its own output
            # can be fed back in; undo that to see what the container will receive.
            actual = environment[key].replace("$$", "$")
            with self.subTest(key=key):
                self.assertEqual(actual, original)

    def test_compose_env_interpolates_quoted_values(self) -> None:
        update_env_file(self.dir / ".env.production", {"A": "1"})
        update_env_file(self.dir / "compose.env", {"APP_VERSION": "1.2.3-rc.1", "PORT": "2570"})
        service = self.config()["services"]["app"]  # type: ignore[index]
        self.assertEqual(service["image"], "busybox:1.2.3-rc.1")
        self.assertEqual(service["ports"][0]["published"], "2570")
        self.assertEqual(service["ports"][0]["host_ip"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
