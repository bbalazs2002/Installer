"""The GitHub token file (``~/.config/installer/token``, mode 600).

The token is deliberately kept out of the password vault: it is needed at startup, before a
master password could be asked for (specification 8.2).
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import InstallerError
from .fsutil import atomic_write_text

TOKEN_MODE = 0o600
_DIR_MODE = 0o700


class TokenStore:
    """Reads and writes the token. ``legacy`` is an older location that is still *read* when the
    current file does not exist (a token saved by an earlier version keeps working); new tokens
    are always written to ``path``."""

    def __init__(self, path: Path, legacy: Path | None = None) -> None:
        self._path = path
        self._legacy = legacy

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> str | None:
        """The stored token, or ``None`` if there is none (or the file is empty)."""
        for candidate in (self._path, self._legacy):
            if candidate is None:
                continue
            try:
                token = candidate.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                raise InstallerError(f"The token file cannot be read: {exc}") from exc
            return token or None
        return None

    def save(self, token: str) -> None:
        token = token.strip()
        if not token or any(ch.isspace() for ch in token):
            raise InstallerError("Invalid token.")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        atomic_write_text(self._path, token + "\n", TOKEN_MODE)

    def delete(self) -> None:
        for candidate in (self._path, self._legacy):
            if candidate is None:
                continue
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
