import contextlib
import io
import json
import unittest
from typing import Any
from unittest import mock

from installer import __version__
from installer.cli import main
from installer.state import load_install_info
from installer.ui.scripted import ScriptedUi

from .appsupport import REPO, AppTestCase


class CliTestCase(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The installer refuses to run as root; tests may run as root (e.g. in a container).
        patcher = mock.patch("installer.cli.os.geteuid", return_value=1000, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv: str, ui: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                list(argv),
                ui=ui,
                docker=self.docker,
                github_factory=lambda token: self.github,
                health=self.fake_health,
                paths=self.paths,
            )
        return code, out.getvalue(), err.getvalue()


class BasicTests(CliTestCase):
    def test_version(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_running_as_root_is_refused(self) -> None:
        with mock.patch("installer.cli.os.geteuid", return_value=0, create=True):
            code, _, err = self.run_cli("status")
        self.assertEqual(code, 3)
        self.assertIn("root", err)

    def test_menu_needs_an_interactive_terminal(self) -> None:
        code, _, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("interaktív terminál", err)

    def test_menu_runs_with_an_injected_ui_and_quits(self) -> None:
        code, _, _ = self.run_cli(ui=ScriptedUi(["quit"]))
        self.assertEqual(code, 0)

    def test_missing_state_directory_explains_how_to_create_it(self) -> None:
        import shutil

        shutil.rmtree(self.state_dir)
        code, _, err = self.run_cli(ui=ScriptedUi(["quit"]))
        self.assertEqual(code, 3)
        self.assertIn("sudo mkdir -p", err)

    def test_escape_in_the_main_menu_is_a_clean_exit(self) -> None:
        from installer.ui.base import UiBack

        self.assertEqual(self.run_cli(ui=ScriptedUi([UiBack]))[0], 0)


class StatusTests(CliTestCase):
    def test_status_without_installations(self) -> None:
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Még nincs telepített alkalmazás", out)

    def test_status_json_has_no_secrets(self) -> None:
        self.run_install("0.1.0")
        secret = self.env()["APIKEY_MASTER_SECRET"]
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        (item,) = data["installs"]
        self.assertEqual(
            (item["name"], item["version"], item["port"], item["health"]),
            ("investment", "0.1.0", 2570, "ok"),
        )
        self.assertNotIn(secret, out)

    def test_status_text(self) -> None:
        self.run_install("0.1.0")
        _, out, _ = self.run_cli("status")
        self.assertIn("investment  0.1.0", out)
        self.assertIn("állapot: ok", out)

    def test_status_never_asks_a_question(self) -> None:
        # The batch UI raises on any question; status must therefore never need one.
        self.run_install("0.1.0")
        self.assertEqual(self.run_cli("status")[0], 0)


class UpdatesTests(CliTestCase):
    def test_updates_lists_available_and_current(self) -> None:
        self.run_install("0.1.0")
        self.assertIn("naprakész", self.run_cli("updates")[1])
        self.publish("0.2.0")
        code, out, _ = self.run_cli("updates")
        self.assertEqual(code, 0)
        self.assertIn("investment  0.1.0 → 0.2.0", out)

    def test_updates_without_a_token(self) -> None:
        self.run_install("0.1.0")
        self.tokens.delete()
        code, _, err = self.run_cli("updates")
        self.assertEqual(code, 1)
        self.assertIn("Nincs GitHub-token", err)


class PlanTests(CliTestCase):
    def test_plan_prints_and_changes_nothing(self) -> None:
        self.publish("0.1.0")
        code, out, _ = self.run_cli("plan", REPO, "0.1.0", str(self.target))
        self.assertEqual(code, 0)
        self.assertIn("Új telepítés", out)
        self.assertIn("Lépések:", out)
        self.assertIsNone(load_install_info(self.target))
        self.assertEqual(self.docker.names().count("compose_up"), 0)
        self.assertEqual(self.docker.names().count("load_image"), 0)
        self.assertEqual(list(self.paths.tmp_dir.iterdir()), [])

    def test_plan_for_an_update(self) -> None:
        self.run_install("0.1.0")
        self.publish("0.2.0")
        code, out, _ = self.run_cli("plan", REPO, "v0.2.0", str(self.target))
        self.assertEqual(code, 0)
        self.assertIn("Ez frissítés, nem új telepítés: 0.1.0 → 0.2.0", out)

    def test_blocked_plan_exits_nonzero(self) -> None:
        (self.target / "stranger.txt").write_text("x", encoding="utf-8")
        self.publish("0.1.0")
        code, out, _ = self.run_cli("plan", REPO, "0.1.0", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("Akadályok:", out)

    def test_unknown_version_and_garbage(self) -> None:
        self.publish("0.1.0")
        code, _, err = self.run_cli("plan", REPO, "9.9.9", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("nem található", err)
        code, _, err = self.run_cli("plan", REPO, "not-a-version", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("Érvénytelen verzió", err)


if __name__ == "__main__":
    unittest.main()
