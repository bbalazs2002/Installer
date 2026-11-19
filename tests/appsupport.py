"""Shared fixture for the menu-flow and CLI tests: a Context wired to fakes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from telepito.app import Context
from telepito.envconfig import DefaultEnvConfigurator
from telepito.envfile import read_env
from telepito.health import HealthResponse
from telepito.logging_ import Logger
from telepito.paths import Paths
from telepito.tokenstore import TokenStore
from telepito.ui.scripted import ScriptedUi

from .fake_docker import FakeDocker
from .fake_release import MemoryGitHub, ReleaseSpec
from .test_installer import FakeDb
from .test_wizard import MemoryVault

REPO = "o/investment"
TOKEN = "github_pat_TESTTOKEN123456"
D = ScriptedUi.DEFAULT


class AppTestCase(unittest.TestCase):
    """A fully faked world: GitHub, Docker, database and the running application."""

    with_database = False

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_dir = self.root / "telepito"
        self.state_dir.mkdir()
        self.target = self.root / "investment"
        self.target.mkdir()
        self.paths = Paths(self.state_dir, self.root / "config" / "token")
        self.tokens = TokenStore(self.paths.token_file)
        self.tokens.save(TOKEN)

        self.github = MemoryGitHub()
        self.docker = FakeDocker()
        self.ui = ScriptedUi()
        self.log = Logger(None)
        self.db = FakeDb()
        self.reported_version: str | None = None
        self.clock_value = 0.0
        self.ctx = Context(
            paths=self.paths,
            ui=self.ui,
            log=self.log,
            docker=self.docker,
            tokens=self.tokens,
            github_factory=lambda token: self.github,
            health=self.fake_health,
            sleep=self.fake_sleep,
            monotonic=lambda: self.clock_value,
            port_in_use=lambda port: False,
            disk_free=lambda path: 100 * 2**30,
            env_configurator=DefaultEnvConfigurator(self.log.redactor),
            db_ops=self.db,
        )
        self.ctx._vault = MemoryVault()
        self.docker.on_up = self.app_follows_compose

    # fake application ---------------------------------------------------------------

    def fake_health(self, url: str) -> HealthResponse:
        if self.reported_version is None:
            raise OSError("connection refused")
        body = json.dumps({"status": "ok", "version": self.reported_version}).encode()
        return HealthResponse(200, body)

    def fake_sleep(self, seconds: float) -> None:
        self.clock_value += seconds

    def app_follows_compose(self, project: str, compose: Path, env: Path) -> None:
        self.reported_version = read_env(env)["APP_VERSION"]

    # helpers -------------------------------------------------------------------------

    def publish(self, version: str, **kwargs: Any) -> Any:
        spec = ReleaseSpec(version, migrations=kwargs.pop("migrations", ("m1",)))
        if not self.with_database:
            spec.manifest["database"] = {"required": False}
        for key, value in kwargs.pop("manifest_edits", {}).items():
            spec.manifest[key] = value
        spec.prerelease = kwargs.pop("prerelease", False)
        release = self.github.add(spec, **kwargs)
        self.docker.archive_images = [f"investment:{version}"]
        return release

    def run_install(self, version: str, **kwargs: Any) -> Any:
        """Install ``version`` through the Installer directly (set-up, not under test)."""
        import shutil

        from telepito.installer import InstallJob
        from telepito.plan import PlanRequest, build_plan
        from telepito.state import inspect_target
        from telepito.versions import Version

        release = self.publish(version, **kwargs)
        installer = self.ctx.installer()
        meta_dir = self.root / f"meta-{version}"
        meta = installer.fetch_metadata(REPO, release, meta_dir)
        plan = build_plan(
            PlanRequest(
                REPO,
                Version.parse(version),
                str(self.target),
                meta.manifest,
                inspect_target(self.target),
                requested_migrations=meta.migrations,
            )
        )
        result = installer.install(InstallJob(REPO, release, meta, plan, self.target, 2570))
        shutil.rmtree(meta_dir, ignore_errors=True)
        return result

    def call(self, flow: Any, *args: Any) -> None:
        """Run a flow; Esc ("back") ending it is the normal way out and not an error."""
        from telepito.ui.base import UiBack

        try:
            flow(self.ctx, *args)
        except UiBack:
            pass

    def messages(self) -> str:
        return "\n".join(self.ui.messages())

    def env(self) -> dict[str, str]:
        return read_env(self.target / "shared" / ".env.production")
