import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path

from telepito import __version__
from telepito.app import run_menu
from telepito.flows_misc import selfupdate_screen
from telepito.logging_ import Logger
from telepito.selfupdate import (
    SELF_REPO,
    SelfUpdateError,
    apply_update,
    find_update,
    running_pyz,
)
from telepito.ui.base import UiBack

from .appsupport import AppTestCase
from .fake_release import MemoryGitHub

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "build_pyz.py"


def run_python(*args: str) -> "subprocess.CompletedProcess[str]":
    """Run the current interpreter with UTF-8 I/O, whatever the console's code page is."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )  # fmt: skip


def load_builder():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("build_pyz", SCRIPTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_installer(
    directory: Path, version: str, *, exit_code: int = 0, shown: str | None = None
) -> bytes:
    """A tiny but valid zipapp that behaves like ``telepito --version``."""
    app = directory / f"app-{version}-{exit_code}"
    app.mkdir(exist_ok=True)
    shown_text = shown if shown is not None else f"telepito {version}"
    (app / "__main__.py").write_text(
        f"import sys\nprint({shown_text!r})\nsys.exit({exit_code})\n", encoding="utf-8"
    )
    target = directory / f"fake-{version}-{exit_code}.pyz"
    zipapp.create_archive(app, target)
    return target.read_bytes()


class SelfUpdateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.install_dir = self.dir / "telepito"
        self.install_dir.mkdir()
        self.pyz = self.install_dir / "telepito.pyz"
        self.pyz.write_bytes(b"OLD-INSTALLER")
        os.chmod(self.pyz, 0o750)
        self.github = MemoryGitHub()
        self.log = Logger(None)

    def publish(self, version: str, *, pyz: bytes | None = None, tamper: bool = False,
                prerelease: bool = False, omit_sums_entry: bool = False) -> object:  # fmt: skip
        data = pyz if pyz is not None else fake_installer(self.dir, version)
        sums = f"{hashlib.sha256(data).hexdigest()}  telepito.pyz\n"
        if omit_sums_entry:
            sums = f"{'0' * 64}  something-else\n"
        if tamper:
            data += b"X"
        return self.github.add_raw(
            f"v{version}",
            {"telepito.pyz": data, "SHA256SUMS": sums.encode()},
            prerelease=prerelease,
        )


class FindUpdateTests(SelfUpdateTestCase):
    def test_newer_stable_release_is_found(self) -> None:
        self.publish("9.0.0")
        release = find_update(self.github, "0.1.0")
        assert release is not None
        self.assertEqual(release.tag, "v9.0.0")

    def test_same_or_older_is_not_an_update(self) -> None:
        self.publish("0.1.0")
        self.assertIsNone(find_update(self.github, "0.1.0"))
        self.assertIsNone(find_update(self.github, "0.2.0"))

    def test_prereleases_are_never_offered(self) -> None:
        self.publish("9.0.0-rc.1", prerelease=True)
        self.assertIsNone(find_update(self.github, "0.1.0"))

    def test_no_releases(self) -> None:
        self.assertIsNone(find_update(self.github, "0.1.0"))

    def test_the_repository_is_the_installers_own(self) -> None:
        self.assertEqual(SELF_REPO, "bbalazs2002/Installer")


class ApplyUpdateTests(SelfUpdateTestCase):
    def test_successful_update_keeps_the_old_file_and_the_mode(self) -> None:
        release = self.publish("9.0.0")
        result = apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertEqual((result.old_version, result.new_version), (__version__, "9.0.0"))
        self.assertNotEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")
        self.assertEqual(result.previous_file.read_bytes(), b"OLD-INSTALLER")
        self.assertEqual(result.previous_file.name, "telepito.pyz.previous")
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.pyz.stat().st_mode), 0o750)
        self.assertEqual(sorted(p.name for p in self.install_dir.iterdir()),
                         ["telepito.pyz", "telepito.pyz.previous"])  # fmt: skip

    def test_the_new_file_really_runs(self) -> None:
        release = self.publish("9.0.0")
        apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        out = run_python(str(self.pyz), "--version")
        self.assertEqual(out.stdout.strip(), "telepito 9.0.0")

    def test_a_tampered_download_changes_nothing(self) -> None:
        release = self.publish("9.0.0", tamper=True)
        with self.assertRaises(SelfUpdateError) as ctx:
            apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")
        self.assertEqual([p.name for p in self.install_dir.iterdir()], ["telepito.pyz"])

    def test_missing_checksum_entry(self) -> None:
        release = self.publish("9.0.0", omit_sums_entry=True)
        with self.assertRaises(SelfUpdateError):
            apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_a_release_without_the_files(self) -> None:
        release = self.github.add_raw("v9.0.0", {"notes.txt": b"x"})
        with self.assertRaises(SelfUpdateError):
            apply_update(self.github, release, self.pyz, self.log)

    def test_a_build_that_crashes_is_refused(self) -> None:
        release = self.publish("9.0.0", pyz=fake_installer(self.dir, "9.0.0", exit_code=3))
        with self.assertRaises(SelfUpdateError) as ctx:
            apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertIn("nem fut", str(ctx.exception))
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_a_build_that_reports_another_version_is_refused(self) -> None:
        wrong = fake_installer(self.dir, "9.0.0", shown="telepito 1.2.3")
        release = self.publish("9.0.0", pyz=wrong)
        with self.assertRaises(SelfUpdateError) as ctx:
            apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertIn("más verziót", str(ctx.exception))
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_garbage_that_is_not_a_zip_is_refused(self) -> None:
        release = self.publish("9.0.0", pyz=b"this is not a zipapp")
        with self.assertRaises(SelfUpdateError):
            apply_update(self.github, release, self.pyz, self.log)  # type: ignore[arg-type]
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_only_a_pyz_can_be_updated(self) -> None:
        release = self.publish("9.0.0")
        source_file = self.install_dir / "cli.py"
        source_file.write_text("x", encoding="utf-8")
        with self.assertRaises(SelfUpdateError):
            apply_update(self.github, release, source_file, self.log)  # type: ignore[arg-type]

    def test_an_unwritable_directory_is_reported(self) -> None:
        release = self.publish("9.0.0")
        missing = self.dir / "gone" / "telepito.pyz"
        with self.assertRaises(SelfUpdateError):
            apply_update(self.github, release, missing, self.log)  # type: ignore[arg-type]


class RunningPyzTests(unittest.TestCase):
    def test_none_outside_a_zipapp(self) -> None:
        self.assertIsNone(running_pyz())  # the test runner is not a .pyz


class BuildTests(unittest.TestCase):
    def test_the_built_archive_runs_and_keeps_exit_codes(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            out = builder.build(Path(tmp) / "dist" / "telepito.pyz")
            shown = run_python(str(out), "--version")
            self.assertEqual(shown.stdout.strip(), f"telepito {__version__}")
            # argparse rejects a missing argument with exit code 2: it must reach the shell
            bad = run_python(str(out), "plan")
            self.assertEqual(bad.returncode, 2)
            state = Path(tmp) / "state"
            state.mkdir()
            status = run_python(str(out), "--state-dir", str(state), "status")
            # The installer refuses to run as root (exit code 3); anyone else gets a clean 0.
            is_root = hasattr(os, "geteuid") and os.geteuid() == 0
            self.assertEqual(status.returncode, 3 if is_root else 0, status.stderr)

    def test_the_archive_contains_only_the_package(self) -> None:
        import zipfile

        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            out = builder.build(Path(tmp) / "telepito.pyz")
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
            self.assertIn("__main__.py", names)
            self.assertIn("telepito/cli.py", names)
            self.assertFalse([n for n in names if "__pycache__" in n or n.startswith("tests")])
            self.assertEqual(out.read_bytes()[:21], b"#!/usr/bin/env python")

    def test_the_package_has_no_third_party_imports(self) -> None:
        import ast

        root = Path(__file__).resolve().parent.parent / "src" / "telepito"
        allowed = set(sys.stdlib_module_names)
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertIn(name, allowed | {"telepito"})


class SelfUpdateFlowTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install_dir = self.root / "bin"
        self.install_dir.mkdir()
        self.pyz = self.install_dir / "telepito.pyz"
        self.pyz.write_bytes(b"OLD-INSTALLER")
        self.ctx.pyz_path = self.pyz

    def publish_self(self, version: str) -> None:
        data = fake_installer(self.root, version)
        sums = f"{hashlib.sha256(data).hexdigest()}  telepito.pyz\n".encode()
        self.github.add_raw(f"v{version}", {"telepito.pyz": data, "SHA256SUMS": sums})

    def test_up_to_date(self) -> None:
        selfupdate_screen(self.ctx)
        self.assertIn("naprakész", self.messages())
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_update_confirmed_exits_the_menu(self) -> None:
        self.publish_self("9.0.0")
        self.ui.push("selfupdate", True)
        self.assertEqual(run_menu(self.ctx), 0)  # returned on its own: no "quit" was needed
        self.assertNotEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")
        self.assertIn("indítsd újra", self.messages())

    def test_update_declined(self) -> None:
        self.publish_self("9.0.0")
        self.ui.push(False)
        selfupdate_screen(self.ctx)
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_escape_at_the_question(self) -> None:
        self.publish_self("9.0.0")
        self.ui.push(UiBack)
        with self.assertRaises(UiBack):
            selfupdate_screen(self.ctx)
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_a_development_checkout_cannot_be_updated(self) -> None:
        self.ctx.pyz_path = None
        selfupdate_screen(self.ctx)
        self.assertIn("nem a telepito.pyz", self.messages())

    def test_failures_are_shown_through_the_menu(self) -> None:
        data = fake_installer(self.root, "9.0.0")
        sums = f"{hashlib.sha256(data).hexdigest()}  telepito.pyz\n".encode()
        self.github.add_raw("v9.0.0", {"telepito.pyz": data + b"X", "SHA256SUMS": sums})
        self.ui.push("selfupdate", True, "quit")
        run_menu(self.ctx)
        self.assertIn("SHA-256", self.messages())
        self.assertEqual(self.pyz.read_bytes(), b"OLD-INSTALLER")

    def test_the_main_menu_shows_the_version(self) -> None:
        self.ui.push("quit")
        run_menu(self.ctx)
        menu = next(c for c in self.ui.calls if c.kind == "menu")
        self.assertIn(f"telepito {__version__}", menu.text)


if __name__ == "__main__":
    unittest.main()
