import tempfile
import unittest
from pathlib import Path

from telepito.envconfig import EnvConfigError
from telepito.envfile import read_env, update_env_file
from telepito.envmanage import MASKED, NOT_SET, describe_env, edit_value, rotate_secret
from telepito.errors import UserAbort
from telepito.logging_ import Redactor
from telepito.ui.scripted import ScriptedUi

from .test_wizard import ENV, MemoryVault, manifest


class EnvManageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".env.production"
        self.manifest = manifest(ENV, database=True)
        self.redactor = Redactor()
        self.old_secret = "old-secret-value-0123456789"
        update_env_file(
            self.path,
            {
                "NODE_ENV": "production",
                "BASE_PATH": "/investment/",
                "APIKEY_MASTER_SECRET": self.old_secret,
                "DATABASE_URL": "postgresql://u:dbpass@h/db",
                "HAND_ADDED": "whatever",
            },
        )


class DescribeTests(EnvManageTestCase):
    def test_secrets_are_masked_and_plain_values_shown(self) -> None:
        rows = {r.name: r for r in describe_env(self.manifest, read_env(self.path))}
        self.assertEqual(rows["BASE_PATH"].display, "/investment/")
        self.assertEqual(rows["APIKEY_MASTER_SECRET"].display, MASKED)
        self.assertEqual(rows["DATABASE_URL"].display, MASKED)
        self.assertEqual(rows["TWELVEDATA_API_KEY"].display, NOT_SET)
        self.assertEqual(rows["INTRADAY"].group, "advanced")

    def test_unknown_variables_are_treated_as_secret(self) -> None:
        rows = {r.name: r for r in describe_env(self.manifest, read_env(self.path))}
        self.assertEqual(rows["HAND_ADDED"].display, MASKED)
        self.assertEqual(rows["HAND_ADDED"].group, "other")

    def test_no_secret_value_appears_anywhere_in_the_rows(self) -> None:
        text = repr(describe_env(self.manifest, read_env(self.path)))
        for secret in (self.old_secret, "dbpass", "whatever"):
            self.assertNotIn(secret, text)

    def test_fixed_and_database_rows_are_not_editable(self) -> None:
        rows = {r.name: r for r in describe_env(self.manifest, read_env(self.path))}
        self.assertFalse(rows["NODE_ENV"].editable)
        self.assertFalse(rows["DATABASE_URL"].editable)
        self.assertTrue(rows["BASE_PATH"].editable)


class EditTests(EnvManageTestCase):
    def test_valid_edit(self) -> None:
        edit_value(self.path, self.manifest, "BASE_PATH", "/new/", self.redactor)
        self.assertEqual(read_env(self.path)["BASE_PATH"], "/new/")
        self.assertEqual(read_env(self.path)["HAND_ADDED"], "whatever")  # untouched

    def test_invalid_edits_are_refused_and_change_nothing(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        for name, value in [
            ("BASE_PATH", "no-slashes"),
            ("NODE_ENV", "development"),
            ("NOT_DECLARED", "x"),
            ("APIKEY_MASTER_SECRET", "short"),
            ("BASE_PATH", "it's"),
        ]:
            with self.subTest(name=name, value=value), self.assertRaises(EnvConfigError):
                edit_value(self.path, self.manifest, name, value, self.redactor)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_edited_secret_is_masked_afterwards(self) -> None:
        new = "brand-new-secret-value-1234"
        edit_value(self.path, self.manifest, "APIKEY_MASTER_SECRET", new, self.redactor)
        self.assertEqual(self.redactor.redact(new), "***")


class RotateTests(EnvManageTestCase):
    def rotate(self, ui: ScriptedUi, vault: MemoryVault | None, name: str) -> bool:
        provider = (lambda: vault) if vault is not None else None
        return rotate_secret(ui, self.manifest, self.path, name, self.redactor, provider)

    def test_generated_rotation_updates_env_and_vault(self) -> None:
        vault = MemoryVault()
        vault.set("investment", "APIKEY_MASTER_SECRET", self.old_secret)
        ui = ScriptedUi([True, "generate"])
        self.assertTrue(self.rotate(ui, vault, "APIKEY_MASTER_SECRET"))
        new = read_env(self.path)["APIKEY_MASTER_SECRET"]
        self.assertNotEqual(new, self.old_secret)
        self.assertGreaterEqual(len(new), 48)
        self.assertEqual(vault.get("investment", "APIKEY_MASTER_SECRET"), new)
        self.assertEqual(vault.saves, 1)
        self.assertEqual(self.redactor.redact(new), "***")

    def test_rotation_warning_is_shown_before_confirming(self) -> None:
        ui = ScriptedUi([True, "generate"])
        self.rotate(ui, MemoryVault(), "APIKEY_MASTER_SECRET")
        confirm = next(c for c in ui.calls if c.kind == "confirm")
        self.assertIn("Csere után a kulcsok elvesznek.", confirm.text)
        self.assertIn("újraindítás", confirm.text)

    def test_the_stored_old_value_is_not_offered_as_the_new_one(self) -> None:
        vault = MemoryVault()
        vault.set("investment", "APIKEY_MASTER_SECRET", self.old_secret)
        ui = ScriptedUi([True, "generate"])
        self.rotate(ui, vault, "APIKEY_MASTER_SECRET")
        menu = next(c for c in ui.calls if c.kind == "menu")
        self.assertNotIn("vault", menu.items)

    def test_manual_rotation(self) -> None:
        typed = "typed new secret value 12345"
        ui = ScriptedUi([True, "manual", typed, typed])
        self.rotate(ui, None, "APIKEY_MASTER_SECRET")
        self.assertEqual(read_env(self.path)["APIKEY_MASTER_SECRET"], typed)

    def test_declining_changes_nothing(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        vault = MemoryVault()
        with self.assertRaises(UserAbort):
            self.rotate(ScriptedUi([False]), vault, "APIKEY_MASTER_SECRET")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(vault.saves, 0)

    def test_optional_secret_can_be_cleared(self) -> None:
        update_env_file(self.path, {"TWELVEDATA_API_KEY": "abcd-efgh-1234"})
        self.rotate(ScriptedUi([True, "skip"]), MemoryVault(), "TWELVEDATA_API_KEY")
        self.assertNotIn("TWELVEDATA_API_KEY", read_env(self.path))

    def test_non_secret_and_undeclared_cannot_be_rotated(self) -> None:
        for name in ("BASE_PATH", "NOPE"):
            with self.subTest(name=name), self.assertRaises(EnvConfigError):
                self.rotate(ScriptedUi(), None, name)


if __name__ == "__main__":
    unittest.main()
