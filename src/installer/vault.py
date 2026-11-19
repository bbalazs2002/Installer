"""The encrypted password vault (specification 8.3).

File layout::

    TELEPITO-VAULT 1 scrypt N=32768 r=8 p=1 <32 hex chars of salt>\\n   (header, plain text)
    <openssl enc output: "Salted__" + salt + AES-256-CBC ciphertext>
    <32 bytes: HMAC-SHA256 over header + ciphertext>

Keys come from ``scrypt(NFKC(master password), salt)`` -> 64 bytes: 32 for the cipher, 32 for
the MAC. Encryption is *encrypt-then-MAC*: opening verifies the MAC (constant time) first, and
only then decrypts. Only the standard library and the ``openssl`` command line are used. The
cipher key is handed to ``openssl`` through a file descriptor (a private temporary file on
Windows, which has no ``pass_fds``), never through arguments or the environment.

The GitHub token is deliberately *not* stored here (it is needed at startup, before any master
password could be asked for).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

from .errors import PrerequisiteError, TelepitoError
from .fsutil import atomic_write_bytes
from .logging_ import Redactor
from .validators import is_app_name, is_env_name

MAGIC = "TELEPITO-VAULT"
FORMAT_VERSION = 1
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * 1024 * 1024
SALT_LEN = 16
MAC_LEN = 32
OPENSSL_ITER = 1000
MIN_PASSWORD_LENGTH = 12
VAULT_FILE_MODE = 0o600
OPENSSL_TIMEOUT = 30
BACKOFF_CAP = 30.0

_HEADER_RE = re.compile(
    rf"^{MAGIC} (\d+) scrypt N={SCRYPT_N} r={SCRYPT_R} p={SCRYPT_P} ([0-9a-f]{{{SALT_LEN * 2}}})$"
)
_MSG_AUTH = "Wrong password or damaged vault."


class VaultError(TelepitoError):
    """The vault cannot be used (unsupported format, missing openssl, misuse)."""


class VaultAuthError(VaultError):
    """Wrong master password *or* a damaged file (deliberately not distinguished)."""

    def __init__(self) -> None:
        super().__init__(_MSG_AUTH)


def validate_master_password(password: str) -> str | None:
    """An error message if the password is too weak to create a vault with, else ``None``."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The master password must be at least {MIN_PASSWORD_LENGTH} characters long."
    return None


def _derive_keys(password: str, salt: bytes) -> tuple[str, bytes]:
    """Return (cipher key as hex text for ``openssl -pass``, MAC key)."""
    material = hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=64,
    )
    return material[:32].hex(), material[32:]


def _openssl() -> str:
    exe = shutil.which("openssl")
    if exe is None:
        raise PrerequisiteError("The openssl command was not found; the vault cannot be used.")
    return exe


@contextlib.contextmanager
def _key_argument(key_hex: str) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    """Yield the ``-pass`` argument and the file descriptors to inherit (``pass_fds``)."""
    payload = (key_hex + "\n").encode("ascii")
    if sys.platform != "win32":
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, payload)  # 65 bytes fit the pipe buffer: no reader needed yet
            os.close(write_fd)
            write_fd = -1
            yield ["-pass", f"fd:{read_fd}"], (read_fd,)
        finally:
            if write_fd != -1:
                os.close(write_fd)
            os.close(read_fd)
        return
    # Windows (development machines only): no pass_fds, so use a private temp file that
    # exists just for the duration of the call.
    fd, name = tempfile.mkstemp(prefix="tp-key-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        yield ["-pass", f"file:{name}"], ()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _openssl_run(data: bytes, key_hex: str, *, decrypt: bool) -> bytes:
    base = [_openssl(), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(OPENSSL_ITER)]
    base += ["-md", "sha256"] + (["-d"] if decrypt else [])
    with _key_argument(key_hex) as (pass_args, inherited_fds):
        try:
            completed = subprocess.run(
                [*base, *pass_args],
                input=data,
                capture_output=True,
                timeout=OPENSSL_TIMEOUT,
                shell=False,
                check=False,
                pass_fds=inherited_fds,
            )
        except subprocess.TimeoutExpired as exc:
            raise VaultError("openssl timed out.") from exc
    if completed.returncode != 0:
        raise VaultError("Encrypting or decrypting the vault failed.")
    output: bytes = completed.stdout
    return output


def _header(salt: bytes) -> bytes:
    line = f"{MAGIC} {FORMAT_VERSION} scrypt N={SCRYPT_N} r={SCRYPT_R} p={SCRYPT_P} {salt.hex()}\n"
    return line.encode("ascii")


def _check_name(installation: str, name: str | None = None) -> None:
    if not is_app_name(installation):
        raise VaultError(f"Invalid installation name: {installation!r}")
    if name is not None and not is_env_name(name):
        raise VaultError(f"Invalid secret name: {name!r}")


class Vault:
    """The decrypted contents plus what is needed to save them again."""

    def __init__(
        self, path: Path, password: str, entries: dict[str, dict[str, str]] | None = None
    ) -> None:
        self._path = path
        self._password = password
        self._entries: dict[str, dict[str, str]] = entries if entries is not None else {}

    def __repr__(self) -> str:
        return f"Vault({self._path.name}, {len(self._entries)} installations)"

    # ------------------------------------------------------------ construction

    @staticmethod
    def exists(path: Path) -> bool:
        return path.is_file()

    @classmethod
    def create(cls, path: Path, password: str) -> Vault:
        """Create a new, empty vault. The password must satisfy the policy."""
        problem = validate_master_password(password)
        if problem is not None:
            raise VaultError(problem)
        if path.exists():
            raise VaultError("The vault already exists.")
        vault = cls(path, password)
        vault.save()
        return vault

    @classmethod
    def open(cls, path: Path, password: str) -> Vault:
        """Open and decrypt. The MAC is verified *before* anything is decrypted."""
        try:
            blob = path.read_bytes()
        except OSError as exc:
            raise VaultError(f"The vault cannot be read: {exc}") from exc
        newline = blob.find(b"\n")
        if newline < 0:
            raise VaultError("The vault format is unknown.")
        header_line = blob[:newline].decode("ascii", errors="replace")
        match = _HEADER_RE.match(header_line)
        if match is None:
            raise VaultError("The vault format or its parameters are not supported.")
        if int(match.group(1)) != FORMAT_VERSION:
            raise VaultError(
                f"The vault format version ({match.group(1)}) is not supported. "
                "Update the installer."
            )
        if len(blob) < newline + 1 + MAC_LEN:
            raise VaultAuthError()
        header = blob[: newline + 1]
        ciphertext = blob[newline + 1 : -MAC_LEN]
        stored_mac = blob[-MAC_LEN:]
        key_hex, mac_key = _derive_keys(password, bytes.fromhex(match.group(2)))
        expected = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, stored_mac):
            raise VaultAuthError()
        try:
            plaintext = _openssl_run(ciphertext, key_hex, decrypt=True)
            data = json.loads(plaintext)
        except (VaultError, ValueError) as exc:
            raise VaultError("The vault contents are damaged.") from exc
        return cls(path, password, _validate_entries(data))

    # ------------------------------------------------------------------ access

    def installations(self) -> list[str]:
        return sorted(self._entries)

    def names(self, installation: str) -> list[str]:
        return sorted(self._entries.get(installation, {}))

    def get(self, installation: str, name: str) -> str | None:
        return self._entries.get(installation, {}).get(name)

    def set(self, installation: str, name: str, value: str) -> None:
        _check_name(installation, name)
        self._entries.setdefault(installation, {})[name] = value

    def delete(self, installation: str, name: str) -> bool:
        group = self._entries.get(installation)
        if group is None or name not in group:
            return False
        del group[name]
        if not group:
            del self._entries[installation]
        return True

    def register_secrets(self, redactor: Redactor) -> None:
        """Mask every stored value in all output from now on."""
        for group in self._entries.values():
            for value in group.values():
                redactor.register(value)

    # ------------------------------------------------------------------- saving

    def save(self) -> None:
        salt = secrets.token_bytes(SALT_LEN)
        key_hex, mac_key = _derive_keys(self._password, salt)
        payload = json.dumps(
            {"version": FORMAT_VERSION, "entries": self._entries}, ensure_ascii=False
        ).encode("utf-8")
        ciphertext = _openssl_run(payload, key_hex, decrypt=False)
        header = _header(salt)
        mac = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
        atomic_write_bytes(self._path, header + ciphertext + mac, VAULT_FILE_MODE)

    def change_password(self, new_password: str) -> None:
        """Re-encrypt with a new master password (and a fresh salt)."""
        problem = validate_master_password(new_password)
        if problem is not None:
            raise VaultError(problem)
        previous, self._password = self._password, new_password
        try:
            self.save()
        except BaseException:
            self._password = previous
            raise

    def export_to(self, destination: Path) -> None:
        """Copy the *encrypted* file; the export is as safe as the vault itself."""
        atomic_write_bytes(destination, self._path.read_bytes(), VAULT_FILE_MODE)

    def merge_from(self, other: Vault, *, overwrite: bool = False) -> list[tuple[str, str]]:
        """Import entries from another opened vault.

        Existing entries are kept unless ``overwrite``; the conflicting
        ``(installation, name)`` pairs are returned either way.
        """
        conflicts: list[tuple[str, str]] = []
        for installation, group in other._entries.items():
            for name, value in group.items():
                current = self.get(installation, name)
                if current is not None and current != value:
                    conflicts.append((installation, name))
                    if not overwrite:
                        continue
                self.set(installation, name, value)
        return conflicts


def _validate_entries(data: object) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise VaultError("The vault contents are damaged.")
    entries: dict[str, dict[str, str]] = {}
    for installation, group in data["entries"].items():
        if (
            not isinstance(installation, str)
            or not isinstance(group, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in group.items())
        ):
            raise VaultError("The vault contents are damaged.")
        entries[installation] = dict(group)
    return entries


def unlock(
    path: Path,
    ask_password: Callable[[int], str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 10,
) -> Vault:
    """Open the vault, asking for the password until it works.

    After each wrong password the wait doubles (1, 2, 4, ... capped at 30 s). ``ask_password``
    may raise (e.g. the UI's "back") to cancel.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        password = ask_password(attempt)
        try:
            return Vault.open(path, password)
        except VaultAuthError:
            if attempt == max_attempts:
                break
            sleep(delay)
            delay = min(delay * 2, BACKOFF_CAP)
    raise VaultAuthError()
