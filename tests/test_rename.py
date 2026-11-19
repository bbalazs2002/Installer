"""The telepito -> installer rename: compatibility with installers of the 0.1.x generation."""

import hashlib
import hmac
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import __version__
from installer.cli import main
from installer.identity import LEGACY_PYZ_NAME, PYZ_NAME, program_name
from installer.logging_ import Logger
from installer.selfupdate import migrate_legacy_name
from installer.ui.scripted import ScriptedUi

from .appsupport import AppTestCase

BUILDER = Path(__file__).resolve().parent.parent / "scripts" / "build_pyz.py"


def build(destination: Path) -> Path:
    spec = importlib.util.spec_from_file_location("build_pyz", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built: Path = module.build(destination)
    return built


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8", env=env
    )


class ProgramNameTests(unittest.TestCase):
    def test_name_follows_the_file_name(self) -> None:
        self.assertEqual(program_name(None), "installer")
        self.assertEqual(program_name(Path("/x/installer.pyz")), "installer")
        self.assertEqual(program_name(Path("/x/telepito.pyz")), "telepito")
        self.assertEqual(program_name(Path("/x/anything.pyz")), "installer")


class CompatibilityWithOldUpdatersTests(unittest.TestCase):
    """What a 0.1.x updater does with a downloaded ``telepito.pyz`` is reproduced here: it runs
    ``python -I <scratch>/telepito.pyz --version`` and demands exactly ``telepito <version>``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.built = build(self.dir / "dist" / PYZ_NAME)

    def test_the_new_name_reports_installer(self) -> None:
        self.assertEqual(
            run("-I", str(self.built), "--version").stdout.strip(), f"installer {__version__}"
        )

    def test_the_same_archive_under_the_old_name_reports_telepito(self) -> None:
        scratch = self.dir / ".telepito-update-abc"
        scratch.mkdir()
        old_name = scratch / LEGACY_PYZ_NAME
        shutil.copy2(self.built, old_name)
        result = run("-I", str(old_name), "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"telepito {__version__}")  # what 0.1.x requires

    def test_both_names_are_byte_identical_so_one_checksum_serves_both(self) -> None:
        copy = self.dir / "dist" / LEGACY_PYZ_NAME
        shutil.copy2(self.built, copy)
        self.assertEqual(
            hashlib.sha256(copy.read_bytes()).digest(),
            hashlib.sha256(self.built.read_bytes()).digest(),
        )

    @unittest.skipIf(sys.platform == "win32", "needs symlinks")
    def test_a_link_with_the_old_name_to_the_new_file_reports_installer(self) -> None:
        link = self.dir / "dist" / LEGACY_PYZ_NAME
        os.symlink(PYZ_NAME, link)
        self.assertEqual(run(str(link), "--version").stdout.strip(), f"installer {__version__}")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.old = self.dir / LEGACY_PYZ_NAME
        self.old.write_bytes(b"ARCHIVE")
        self.log = Logger(None)

    def test_rename_leaves_a_link_behind(self) -> None:
        new = migrate_legacy_name(self.old, self.log)
        self.assertEqual(new, self.dir / PYZ_NAME)
        self.assertEqual(new.read_bytes(), b"ARCHIVE")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.old.resolve(), new.resolve())
        self.assertEqual(self.old.read_bytes(), b"ARCHIVE")  # the old path still works

    def test_the_previous_copy_follows(self) -> None:
        (self.dir / "telepito.pyz.previous").write_bytes(b"OLDER")
        migrate_legacy_name(self.old, self.log)
        self.assertEqual((self.dir / "installer.pyz.previous").read_bytes(), b"OLDER")
        self.assertFalse((self.dir / "telepito.pyz.previous").exists())

    def test_other_names_are_left_alone(self) -> None:
        other = self.dir / "something.pyz"
        other.write_bytes(b"x")
        self.assertEqual(migrate_legacy_name(other, self.log), other)
        self.assertTrue(other.exists() and not other.is_symlink())

    def test_nothing_is_overwritten(self) -> None:
        (self.dir / PYZ_NAME).write_bytes(b"ALREADY THERE")
        self.assertEqual(migrate_legacy_name(self.old, self.log), self.old)
        self.assertEqual((self.dir / PYZ_NAME).read_bytes(), b"ALREADY THERE")
        self.assertFalse(self.old.is_symlink())

    def test_without_symlink_support_the_rename_is_undone(self) -> None:
        with mock.patch("installer.selfupdate.os.symlink", side_effect=OSError("no links")):
            result = migrate_legacy_name(self.old, self.log)
        self.assertEqual(result, self.old)
        self.assertEqual(self.old.read_bytes(), b"ARCHIVE")
        self.assertFalse((self.dir / PYZ_NAME).exists())
        self.assertTrue(any("undone" in line for line in self.log.records))

    def test_a_failing_rename_changes_nothing(self) -> None:
        with mock.patch("installer.selfupdate.os.replace", side_effect=OSError("read-only")):
            self.assertEqual(migrate_legacy_name(self.old, self.log), self.old)
        self.assertEqual(self.old.read_bytes(), b"ARCHIVE")

    def test_running_it_twice_is_harmless(self) -> None:
        new = migrate_legacy_name(self.old, self.log)
        again = migrate_legacy_name(self.old, self.log)  # the old path is a link now
        self.assertEqual(again, self.old)  # name is still telepito.pyz but the target exists
        self.assertEqual(new.read_bytes(), b"ARCHIVE")


class MenuMigrationTests(AppTestCase):
    def test_starting_from_the_old_file_name_renames_it_and_tells_the_user(self) -> None:
        home = self.root / "bin"
        home.mkdir()
        legacy = home / LEGACY_PYZ_NAME
        legacy.write_bytes(b"ARCHIVE")
        ui = ScriptedUi(["quit"])
        with (
            mock.patch("installer.cli.running_pyz", return_value=legacy),
            mock.patch("installer.cli.os.geteuid", return_value=1000, create=True),
        ):
            code = main(
                [],
                ui=ui,
                docker=self.docker,
                github_factory=lambda token: self.github,
                health=self.fake_health,
                paths=self.paths,
            )
        self.assertEqual(code, 0)
        self.assertTrue((home / PYZ_NAME).is_file())
        self.assertTrue(legacy.is_symlink())
        self.assertIn("renamed to installer.pyz", "\n".join(ui.messages()))

    def test_starting_from_the_new_name_asks_nothing_and_renames_nothing(self) -> None:
        home = self.root / "bin2"
        home.mkdir()
        current = home / PYZ_NAME
        current.write_bytes(b"ARCHIVE")
        ui = ScriptedUi(["quit"])
        with (
            mock.patch("installer.cli.running_pyz", return_value=current),
            mock.patch("installer.cli.os.geteuid", return_value=1000, create=True),
        ):
            main(
                [],
                ui=ui,
                docker=self.docker,
                github_factory=lambda token: self.github,
                health=self.fake_health,
                paths=self.paths,
            )
        self.assertEqual(ui.messages(), [])
        self.assertEqual(sorted(p.name for p in home.iterdir()), [PYZ_NAME])


class LegacyVaultTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("openssl") is None, "openssl not installed")
    def test_a_vault_written_with_the_old_magic_still_opens_and_is_upgraded_on_save(self) -> None:
        from installer import vault as vm

        password = "legacy master password"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vault.enc"
            salt = b"\x07" * vm.SALT_LEN
            key_hex, mac_key = vm._derive_keys(password, salt)
            payload = b'{"version": 1, "entries": {"investment": {"DB_PASSWORD": "kept-value-1"}}}'
            ciphertext = vm._openssl_run(payload, key_hex, decrypt=False)
            header = (
                f"{vm.LEGACY_MAGIC} {vm.FORMAT_VERSION} scrypt N={vm.SCRYPT_N} "
                f"r={vm.SCRYPT_R} p={vm.SCRYPT_P} {salt.hex()}\n"
            ).encode("ascii")
            mac = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
            path.write_bytes(header + ciphertext + mac)

            opened = vm.Vault.open(path, password)
            self.assertEqual(opened.get("investment", "DB_PASSWORD"), "kept-value-1")
            opened.save()
            self.assertTrue(path.read_bytes().startswith(b"INSTALLER-VAULT 1 scrypt "))
            self.assertEqual(
                vm.Vault.open(path, password).get("investment", "DB_PASSWORD"), "kept-value-1"
            )


if __name__ == "__main__":
    unittest.main()
