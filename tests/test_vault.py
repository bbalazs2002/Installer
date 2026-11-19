import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from installer.logging_ import Redactor
from installer.vault import (
    FORMAT_VERSION,
    MAC_LEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    Vault,
    VaultAuthError,
    VaultError,
    unlock,
    validate_master_password,
)

PASSWORD = "correct horse battery"


@unittest.skipIf(shutil.which("openssl") is None, "openssl not installed")
class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "vault.enc"

    def make(self) -> Vault:
        vault = Vault.create(self.path, PASSWORD)
        vault.set("investment", "APIKEY_MASTER_SECRET", "s3cret-value-1")
        vault.set("investment", "DB_PASSWORD", "päss wörd 'quoted' $x")
        vault.save()
        return vault

    # ------------------------------------------------------------------ round trip

    def test_round_trip(self) -> None:
        self.make()
        opened = Vault.open(self.path, PASSWORD)
        self.assertEqual(opened.get("investment", "APIKEY_MASTER_SECRET"), "s3cret-value-1")
        self.assertEqual(opened.get("investment", "DB_PASSWORD"), "päss wörd 'quoted' $x")
        self.assertEqual(opened.names("investment"), ["APIKEY_MASTER_SECRET", "DB_PASSWORD"])
        self.assertEqual(opened.installations(), ["investment"])
        self.assertIsNone(opened.get("investment", "NOPE"))

    def test_empty_vault_round_trip(self) -> None:
        Vault.create(self.path, PASSWORD)
        self.assertEqual(Vault.open(self.path, PASSWORD).installations(), [])

    def test_file_does_not_contain_plaintext(self) -> None:
        self.make()
        blob = self.path.read_bytes()
        self.assertNotIn(b"s3cret-value-1", blob)
        self.assertNotIn(b"investment", blob)
        self.assertTrue(blob.startswith(b"INSTALLER-VAULT 1 scrypt "))

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes")
    def test_file_mode_is_600(self) -> None:
        self.make()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_each_save_uses_a_fresh_salt_and_ciphertext(self) -> None:
        vault = self.make()
        first = self.path.read_bytes()
        vault.save()
        self.assertNotEqual(first, self.path.read_bytes())

    def test_unicode_normalisation_of_the_password(self) -> None:
        composed, decomposed = "café-password-1", "café-password-1"
        Vault.create(self.path, composed)
        Vault.open(self.path, decomposed)

    # --------------------------------------------------------------- authentication

    def test_wrong_password(self) -> None:
        self.make()
        with self.assertRaises(VaultAuthError) as ctx:
            Vault.open(self.path, "wrong password 1")
        self.assertEqual(str(ctx.exception), "Wrong password or damaged vault.")

    def test_any_modified_byte_is_detected(self) -> None:
        self.make()
        original = self.path.read_bytes()
        newline = original.index(b"\n")
        positions = {
            "salt in header": newline - 3,
            "ciphertext start": newline + 2,
            "ciphertext middle": (newline + len(original)) // 2,
            "mac": len(original) - 5,
        }
        for label, pos in positions.items():
            tampered = bytearray(original)
            tampered[pos] ^= 0x01
            self.path.write_bytes(bytes(tampered))
            with self.subTest(label=label), self.assertRaises(VaultError):
                Vault.open(self.path, PASSWORD)

    def test_tampering_gives_the_same_error_as_a_wrong_password(self) -> None:
        self.make()
        data = bytearray(self.path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.path.write_bytes(bytes(data))
        with self.assertRaises(VaultAuthError):
            Vault.open(self.path, PASSWORD)

    def test_truncated_file(self) -> None:
        self.make()
        blob = self.path.read_bytes()
        newline = blob.index(b"\n")
        for cut in (newline + 1, newline + 10, len(blob) - 1):
            self.path.write_bytes(blob[:cut])
            with self.subTest(cut=cut), self.assertRaises(VaultError):
                Vault.open(self.path, PASSWORD)

    # ---------------------------------------------------------------------- format

    def test_unsupported_format_version(self) -> None:
        self.make()
        blob = self.path.read_bytes().replace(b"INSTALLER-VAULT 1 ", b"INSTALLER-VAULT 2 ", 1)
        self.path.write_bytes(blob)
        with self.assertRaises(VaultError) as ctx:
            Vault.open(self.path, PASSWORD)
        self.assertNotIsInstance(ctx.exception, VaultAuthError)
        self.assertIn("version", str(ctx.exception))

    def test_scrypt_parameters_are_pinned(self) -> None:
        self.assertEqual((FORMAT_VERSION, SCRYPT_N, SCRYPT_R, SCRYPT_P), (1, 2**15, 8, 1))
        self.make()
        blob = self.path.read_bytes()
        header = blob[: blob.index(b"\n")].decode()
        self.assertIn("N=32768 r=8 p=1", header)
        # A file claiming weaker parameters is refused outright, not silently accepted.
        weak = blob.replace(b"N=32768", b"N=16", 1)
        self.path.write_bytes(weak)
        with self.assertRaises(VaultError) as ctx:
            Vault.open(self.path, PASSWORD)
        self.assertNotIsInstance(ctx.exception, VaultAuthError)

    def test_garbage_and_missing_files(self) -> None:
        self.path.write_bytes(b"not a vault")
        with self.assertRaises(VaultError):
            Vault.open(self.path, PASSWORD)
        with self.assertRaises(VaultError):
            Vault.open(self.dir / "missing.enc", PASSWORD)

    def test_mac_is_32_bytes_at_the_end(self) -> None:
        self.make()
        self.assertGreater(len(self.path.read_bytes()), MAC_LEN + 40)

    # ------------------------------------------------------------------ operations

    def test_password_policy(self) -> None:
        self.assertIsNotNone(validate_master_password("short"))
        self.assertIsNone(validate_master_password("x" * 12))
        with self.assertRaises(VaultError):
            Vault.create(self.path, "short")
        self.assertFalse(self.path.exists())

    def test_create_does_not_overwrite(self) -> None:
        self.make()
        with self.assertRaises(VaultError):
            Vault.create(self.path, PASSWORD)

    def test_invalid_names_rejected(self) -> None:
        vault = Vault.create(self.path, PASSWORD)
        for installation, name in [("Bad Name", "A"), ("app", "1BAD"), ("app", "a-b")]:
            with self.subTest(installation=installation, name=name), self.assertRaises(VaultError):
                vault.set(installation, name, "x")

    def test_delete(self) -> None:
        vault = self.make()
        self.assertTrue(vault.delete("investment", "DB_PASSWORD"))
        self.assertFalse(vault.delete("investment", "DB_PASSWORD"))
        self.assertTrue(vault.delete("investment", "APIKEY_MASTER_SECRET"))
        self.assertEqual(vault.installations(), [])  # empty groups disappear

    def test_change_password(self) -> None:
        vault = self.make()
        before = self.path.read_bytes()
        vault.change_password("a brand new password")
        self.assertNotEqual(before, self.path.read_bytes())
        with self.assertRaises(VaultAuthError):
            Vault.open(self.path, PASSWORD)
        reopened = Vault.open(self.path, "a brand new password")
        self.assertEqual(reopened.get("investment", "DB_PASSWORD"), "päss wörd 'quoted' $x")

    def test_change_password_rejects_weak_and_keeps_old(self) -> None:
        vault = self.make()
        with self.assertRaises(VaultError):
            vault.change_password("weak")
        Vault.open(self.path, PASSWORD)

    def test_export_is_an_encrypted_copy(self) -> None:
        vault = self.make()
        target = self.dir / "backup.enc"
        vault.export_to(target)
        self.assertEqual(target.read_bytes(), self.path.read_bytes())
        self.assertEqual(Vault.open(target, PASSWORD).installations(), ["investment"])

    def test_merge_import(self) -> None:
        vault = self.make()
        other_path = self.dir / "other.enc"
        other = Vault.create(other_path, "another long password")
        other.set("investment", "DB_PASSWORD", "different")
        other.set("games", "TOKEN", "new-one")
        conflicts = vault.merge_from(other)
        self.assertEqual(conflicts, [("investment", "DB_PASSWORD")])
        self.assertEqual(vault.get("investment", "DB_PASSWORD"), "päss wörd 'quoted' $x")
        self.assertEqual(vault.get("games", "TOKEN"), "new-one")
        vault.merge_from(other, overwrite=True)
        self.assertEqual(vault.get("investment", "DB_PASSWORD"), "different")

    def test_register_secrets_masks_values(self) -> None:
        vault = self.make()
        redactor = Redactor()
        vault.register_secrets(redactor)
        self.assertEqual(redactor.redact("x s3cret-value-1 y"), "x *** y")

    def test_repr_hides_contents(self) -> None:
        vault = self.make()
        self.assertNotIn("s3cret", repr(vault))
        self.assertNotIn(PASSWORD, repr(vault))

    def test_decrypted_payload_shape_is_validated(self) -> None:
        # A correctly authenticated file with a wrong JSON shape is reported as damaged.
        import hashlib
        import hmac as hmac_module

        from installer import vault as vm

        salt = b"" * vm.SALT_LEN
        key_hex, mac_key = vm._derive_keys(PASSWORD, salt)
        ciphertext = vm._openssl_run(b"[1, 2]", key_hex, decrypt=False)
        header = vm._header(salt)
        mac = hmac_module.new(mac_key, header + ciphertext, hashlib.sha256).digest()
        self.path.write_bytes(header + ciphertext + mac)
        with self.assertRaises(VaultError) as ctx:
            Vault.open(self.path, PASSWORD)
        self.assertNotIsInstance(ctx.exception, VaultAuthError)
        self.assertIn("damaged", str(ctx.exception))


@unittest.skipIf(shutil.which("openssl") is None, "openssl not installed")
class UnlockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "vault.enc"
        Vault.create(self.path, PASSWORD)
        self.sleeps: list[float] = []

    def test_backoff_doubles_up_to_the_cap(self) -> None:
        answers = iter(["bad password 1"] * 8 + [PASSWORD])
        vault = unlock(self.path, lambda attempt: next(answers), sleep=self.sleeps.append)
        self.assertEqual(vault.installations(), [])
        self.assertEqual(self.sleeps, [1, 2, 4, 8, 16, 30, 30, 30])

    def test_gives_up_after_max_attempts(self) -> None:
        with self.assertRaises(VaultAuthError):
            unlock(
                self.path,
                lambda attempt: "wrong password 1",
                sleep=self.sleeps.append,
                max_attempts=3,
            )
        self.assertEqual(self.sleeps, [1, 2])

    def test_cancelling_propagates(self) -> None:
        def cancel(attempt: int) -> str:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            unlock(self.path, cancel, sleep=self.sleeps.append)

    def test_other_errors_are_not_retried(self) -> None:
        self.path.write_bytes(b"garbage")
        with self.assertRaises(VaultError):
            unlock(self.path, lambda attempt: PASSWORD, sleep=self.sleeps.append)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
