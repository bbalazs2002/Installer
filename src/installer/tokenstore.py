"""The GitHub token file (``~/.config/telepito/token``, mode 600).

The token is deliberately kept out of the password vault: it is needed at startup, before a
master password could be asked for (specification 8.2).
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import TelepitoError
from .fsutil import atomic_write_text

TOKEN_MODE = 0o600
_DIR_MODE = 0o700


class TokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> str | None:
        """The stored token, or ``None`` if there is none (or the file is empty)."""
        try:
            token = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise TelepitoError(f"The token file cannot be read: {exc}") from exc
        return token or None

    def save(self, token: str) -> None:
        token = token.strip()
        if not token or any(ch.isspace() for ch in token):
            raise TelepitoError("Invalid token.")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        atomic_write_text(self._path, token + "\n", TOKEN_MODE)

    def delete(self) -> None:
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
