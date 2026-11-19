import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from telepito.logging_ import Redactor
from telepito.manifest import Manifest, parse_manifest
from telepito.ui.base import UiBack
from telepito.ui.scripted import ScriptedUi
from telepito.vault import Vault, VaultAuthError
from telepito.wizard import InteractiveEnvConfigurator, open_vault_interactive

PASSWORD = "a long master password"


def manifest(env: list[dict[str, Any]], *, database: bool = False) -> Manifest:
    data: dict[str, Any] = {
        "schemaVersion": 1,
        "name": "investment",
        "image": {"name": "investment"},
        "compose": {},
        "port": {"default": 2570},
        "health": {"path": "/healthz"},
        "env": env,
    }
    if database:
        data["database"] = {
            "required": True,
            "urlEnv": "DATABASE_URL",
            "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
        }
    return parse_manifest(data)


class MemoryVault(Vault):
    """A vault that never touches the disk or openssl."""

    def __init__(self) -> None:
        super().__init__(Path("memory.enc"), PASSWORD)
        self.saves = 0

    def save(self) -> None:
        self.saves += 1


ENV = [
    {"name": "NODE_ENV", "fixed": "production"},
    {
        "name": "BASE_PATH",
        "description": "Alútvonal",
        "default": "/investment/",
        "validate": "^/.*/$",
    },
    {
        "name": "APIKEY_MASTER_SECRET",
        "description": "Mester titok",
        "secret": True,
        "required": True,
        "generate": {"bytes": 48},
        "minLength": 20,
        "rotationWarning": "Csere után a kulcsok elvesznek.",
    },
    {"name": "TWELVEDATA_API_KEY", "secret": True, "required": False},
    {"name": "INTRADAY", "default": "false", "validate": "^(true|false)$", "group": "advanced"},
]


class WizardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ui = ScriptedUi()
        self.redactor = Redactor()
        self.vault = MemoryVault()

    def wizard(self, *, with_vault: bool = True) -> InteractiveEnvConfigurator:
        provider = (lambda: self.vault) if with_vault else None
        return InteractiveEnvConfigurator(self.ui, self.redactor, provider)

    def run_new(self, answers: list[Any], env: list[dict[str, Any]] | None = None) -> Any:
        self.ui.push(*answers)
        return self.wizard().configure(manifest(env or ENV), {}, is_new=True)


class NewInstallTests(WizardTestCase):
    def test_defaults_generation_and_advanced_skipped(self) -> None:
        changes = self.run_new(
            [
                "",  # BASE_PATH: Enter takes the default
                "generate",  # APIKEY_MASTER_SECRET
                "skip",  # TWELVEDATA_API_KEY (optional)
                False,  # open the advanced section?
            ]
        )
        values = changes.set
        self.assertEqual(values["NODE_ENV"], "production")
        self.assertEqual(values["BASE_PATH"], "/investment/")
        self.assertGreaterEqual(len(values["APIKEY_MASTER_SECRET"]), 48)
        self.assertEqual(values["INTRADAY"], "false")  # default, never asked
        self.assertNotIn("TWELVEDATA_API_KEY", values)
        self.assertEqual(self.ui.remaining, 0)
        self.assertEqual(changes.remove, ())

    def test_fixed_values_are_never_asked(self) -> None:
        self.run_new(["", "generate", "skip", False])
        asked = [c.title for c in self.ui.calls if c.kind in ("text", "menu", "secret")]
        self.assertNotIn("NODE_ENV", asked)

    def test_generated_secret_is_saved_to_the_vault_and_masked(self) -> None:
        changes = self.run_new(["", "generate", "skip", False])
        secret = changes.set["APIKEY_MASTER_SECRET"]
        self.assertEqual(self.vault.get("investment", "APIKEY_MASTER_SECRET"), secret)
        self.assertEqual(self.vault.saves, 1)
        self.assertEqual(self.redactor.redact(f"x {secret} y"), "x *** y")

    def test_secret_never_appears_in_ui_calls(self) -> None:
        changes = self.run_new(["", "generate", "skip", False])
        secret = changes.set["APIKEY_MASTER_SECRET"]
        self.assertNotIn(secret, repr(self.ui.calls))

    def test_manual_secret_requires_matching_confirmation_and_validity(self) -> None:
        typed = "my own long secret value 123"
        changes = self.run_new(
            [
                "",
                "manual",
                "short",  # too short (minLength 20) -> message, ask again
                typed,
                "mismatch",  # confirmation differs -> message, start over
                typed,
                typed,
                "skip",
                False,
            ]
        )
        self.assertEqual(changes.set["APIKEY_MASTER_SECRET"], typed)
        self.assertEqual(self.vault.get("investment", "APIKEY_MASTER_SECRET"), typed)
        self.assertEqual(len(self.ui.messages()), 2)

    def test_invalid_plain_value_is_asked_again(self) -> None:
        changes = self.run_new(["no-slashes", "/custom/", "generate", "skip", False])
        self.assertEqual(changes.set["BASE_PATH"], "/custom/")
        self.assertEqual(len(self.ui.messages()), 1)

    def test_opening_the_advanced_section_asks_them(self) -> None:
        changes = self.run_new(["", "generate", "skip", True, "true"])
        self.assertEqual(changes.set["INTRADAY"], "true")

    def test_rotation_warning_is_shown_with_the_secret_menu(self) -> None:
        self.run_new(["", "generate", "skip", False])
        menu = next(
            c for c in self.ui.calls if c.kind == "menu" and c.title == "APIKEY_MASTER_SECRET"
        )
        self.assertIn("Csere után", menu.text)

    def test_required_secret_cannot_be_skipped(self) -> None:
        self.run_new(["", "generate", "skip", False])
        menu = next(
            c for c in self.ui.calls if c.kind == "menu" and c.title == "APIKEY_MASTER_SECRET"
        )
        self.assertNotIn("skip", menu.items)
        optional = next(
            c for c in self.ui.calls if c.kind == "menu" and c.title == "TWELVEDATA_API_KEY"
        )
        self.assertIn("skip", optional.items)

    def test_required_plain_value_without_default_cannot_be_empty(self) -> None:
        env = [{"name": "HOSTNAME", "required": True}]
        changes = self.run_new(["", "example.org"], env)
        self.assertEqual(changes.set["HOSTNAME"], "example.org")
        self.assertEqual(len(self.ui.messages()), 1)

    def test_optional_plain_value_may_stay_empty(self) -> None:
        changes = self.run_new([""], [{"name": "NOTE"}])
        self.assertNotIn("NOTE", changes.set)

    def test_escape_aborts_and_saves_nothing(self) -> None:
        self.ui.push("", "generate", UiBack)
        with self.assertRaises(UiBack):
            self.wizard().configure(manifest(ENV), {}, is_new=True)
        self.assertEqual(self.vault.saves, 0)
        self.assertEqual(self.vault.installations(), [])


class VaultUseTests(WizardTestCase):
    def test_secret_can_be_taken_from_the_vault(self) -> None:
        self.vault.set("investment", "APIKEY_MASTER_SECRET", "x" * 40)
        changes = self.run_new(["", "vault", "skip", False])
        self.assertEqual(changes.set["APIKEY_MASTER_SECRET"], "x" * 40)
        self.assertEqual(self.vault.saves, 0)  # nothing new to store

    def test_vault_option_only_offered_when_a_value_exists(self) -> None:
        self.run_new(["", "generate", "skip", False])
        menu = next(
            c for c in self.ui.calls if c.kind == "menu" and c.title == "APIKEY_MASTER_SECRET"
        )
        self.assertNotIn("vault", menu.items)

    def test_stored_value_that_fails_validation_is_refused(self) -> None:
        self.vault.set("investment", "APIKEY_MASTER_SECRET", "too short")
        changes = self.run_new(["", "vault", "generate", "skip", False])
        self.assertNotEqual(changes.set["APIKEY_MASTER_SECRET"], "too short")
        self.assertEqual(len(self.ui.messages()), 1)

    def test_existing_vault_values_are_masked_from_the_start(self) -> None:
        self.vault.set("investment", "APIKEY_MASTER_SECRET", "stored-secret-value-123")
        self.vault.register_secrets(self.redactor)
        self.assertEqual(self.redactor.redact("a stored-secret-value-123 b"), "a *** b")

    def test_without_vault_secrets_are_kept_only_in_env_with_a_notice(self) -> None:
        self.ui.push("", "generate", "skip", False)
        self.wizard(with_vault=False).configure(manifest(ENV), {}, is_new=True)
        self.assertTrue(any("jelszótárba" in m for m in self.ui.messages()))

    def test_vault_is_unlocked_only_once(self) -> None:
        calls: list[int] = []

        def provider() -> Vault:
            calls.append(1)
            return self.vault

        env = [
            {"name": "A_SECRET", "secret": True, "generate": {"bytes": 20}, "required": True},
            {"name": "B_SECRET", "secret": True, "generate": {"bytes": 20}, "required": True},
        ]
        self.ui.push("generate", "generate")
        InteractiveEnvConfigurator(self.ui, self.redactor, provider).configure(
            manifest(env), {}, is_new=True
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.vault.saves, 1)


class UpdateTests(WizardTestCase):
    def test_only_new_variables_are_asked(self) -> None:
        existing = {
            "NODE_ENV": "production",
            "BASE_PATH": "/kept/",
            "APIKEY_MASTER_SECRET": "s" * 40,
        }
        self.ui.push("skip")  # only TWELVEDATA_API_KEY is new (INTRADAY is advanced)
        self.ui.push(False)
        changes = self.wizard().configure(manifest(ENV), existing, is_new=False)
        self.assertNotIn("BASE_PATH", changes.set)
        self.assertNotIn("APIKEY_MASTER_SECRET", changes.set)
        self.assertEqual(changes.set["NODE_ENV"], "production")  # fixed values stay in sync
        self.assertEqual(self.redactor.redact("s" * 40), "***")  # existing secret now masked

    def test_removed_variables_need_confirmation(self) -> None:
        existing = {
            "NODE_ENV": "production",
            "OLD_FLAG": "1",
            "DATABASE_URL": "x",
            "USER_ADDED": "y",
        }
        env = [{"name": "NODE_ENV", "fixed": "production"}]
        previous = ["NODE_ENV", "OLD_FLAG"]
        self.ui.push(True)
        changes = self.wizard().configure(
            manifest(env, database=True), existing, is_new=False, previous_names=previous
        )
        self.assertEqual(changes.remove, ("OLD_FLAG",))  # not DATABASE_URL, not USER_ADDED
        self.assertIn("OLD_FLAG", self.ui.calls[-1].text)

    def test_declining_keeps_the_variables(self) -> None:
        existing = {"OLD_FLAG": "1"}
        self.ui.push(False, False)  # keep advanced closed; do not delete the old variable
        changes = self.wizard().configure(
            manifest([{"name": "X", "default": "1", "group": "advanced"}]),
            existing,
            is_new=False,
            previous_names=["OLD_FLAG"],
        )
        self.assertEqual(changes.remove, ())


@unittest.skipIf(shutil.which("openssl") is None, "openssl not installed")
class OpenVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "vault" / "vault.enc"
        self.redactor = Redactor()
        self.sleeps: list[float] = []

    def test_first_use_creates_the_vault_after_a_warning(self) -> None:
        ui = ScriptedUi([True, "short", "a long master password", "a long master password"])
        vault = open_vault_interactive(ui, self.path, self.redactor, sleep=self.sleeps.append)
        assert vault is not None
        self.assertTrue(self.path.is_file())
        confirm = next(c for c in ui.calls if c.kind == "confirm")
        self.assertIn("NEM állítható", confirm.text)
        self.assertEqual(len(ui.messages()), 1)  # the "too short" complaint

    def test_password_confirmation_mismatch_starts_over(self) -> None:
        ui = ScriptedUi(
            [True, "a long master password", "typo master password", "good master password",
             "good master password"]
        )  # fmt: skip
        vault = open_vault_interactive(ui, self.path, self.redactor, sleep=self.sleeps.append)
        assert vault is not None
        Vault.open(self.path, "good master password")

    def test_declining_creation_returns_none(self) -> None:
        ui = ScriptedUi([False])
        self.assertIsNone(open_vault_interactive(ui, self.path, self.redactor))
        self.assertFalse(self.path.exists())

    def test_unlock_existing_with_retry_and_registers_secrets(self) -> None:
        self.path.parent.mkdir(parents=True)
        vault = Vault.create(self.path, PASSWORD)
        vault.set("investment", "DB_PASSWORD", "stored-db-password")
        vault.save()
        ui = ScriptedUi(["wrong password 1", PASSWORD])
        opened = open_vault_interactive(ui, self.path, self.redactor, sleep=self.sleeps.append)
        assert opened is not None
        self.assertEqual(self.sleeps, [1])
        self.assertEqual(self.redactor.redact("pw stored-db-password"), "pw ***")

    def test_too_many_failures(self) -> None:
        self.path.parent.mkdir(parents=True)
        Vault.create(self.path, PASSWORD)
        ui = ScriptedUi(["wrong password 1"] * 10)
        with self.assertRaises(VaultAuthError):
            open_vault_interactive(ui, self.path, self.redactor, sleep=self.sleeps.append)


if __name__ == "__main__":
    unittest.main()
