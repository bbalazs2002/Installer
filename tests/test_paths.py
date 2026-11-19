import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from installer.cli import main
from installer.errors import PrerequisiteError
from installer.paths import STATE_DIR_ENV, TOKEN_FILE_ENV, default_paths


def pyz(path: str) -> mock._patch:  # type: ignore[type-arg]
    return mock.patch("installer.paths.running_pyz", return_value=Path(path))


class StateDirTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(STATE_DIR_ENV, None)
        os.environ.pop(TOKEN_FILE_ENV, None)

    def test_default_is_the_directory_of_the_running_pyz(self) -> None:
        with pyz("/var/Installer/telepito.pyz"):
            paths = default_paths()
        self.assertEqual(paths.state_dir, Path("/var/Installer"))
        self.assertEqual(paths.installs_file, Path("/var/Installer/installs.json"))
        self.assertEqual(paths.vault_file, Path("/var/Installer/vault/vault.enc"))
        self.assertEqual(paths.logs_dir, Path("/var/Installer/logs"))

    def test_any_location_works_not_just_one_fixed_path(self) -> None:
        for where in [
            "/opt/tools/telepito.pyz",
            "/home/deploy/bin/telepito.pyz",
            "/srv/x/telepito.pyz",
        ]:
            with self.subTest(where=where), pyz(where):
                self.assertEqual(default_paths().state_dir, Path(where).parent)

    def test_argument_beats_environment_beats_the_pyz_directory(self) -> None:
        os.environ[STATE_DIR_ENV] = "/from/env"
        with pyz("/var/Installer/telepito.pyz"):
            self.assertEqual(default_paths().state_dir, Path("/from/env"))
            self.assertEqual(default_paths("/from/arg").state_dir, Path("/from/arg"))

    def test_outside_a_zipapp_a_directory_must_be_given(self) -> None:
        with mock.patch("installer.paths.running_pyz", return_value=None):
            with self.assertRaises(PrerequisiteError) as ctx:
                default_paths()
            self.assertIn("--state-dir", str(ctx.exception))
            self.assertEqual(default_paths("/given").state_dir, Path("/given"))

    def test_token_file_defaults_to_the_users_config_directory(self) -> None:
        with pyz("/var/Installer/telepito.pyz"):
            self.assertEqual(
                default_paths().token_file, Path.home() / ".config" / "installer" / "token"
            )
            os.environ[TOKEN_FILE_ENV] = "/from/env/token"
            self.assertEqual(default_paths().token_file, Path("/from/env/token"))

    def test_cli_reports_an_unknown_directory_instead_of_crashing(self) -> None:
        err = io.StringIO()
        with (
            mock.patch("installer.paths.running_pyz", return_value=None),
            mock.patch("installer.cli.os.geteuid", return_value=1000, create=True),
            contextlib.redirect_stderr(err),
        ):
            code = main(["status"])
        self.assertEqual(code, 3)
        self.assertIn("--state-dir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
