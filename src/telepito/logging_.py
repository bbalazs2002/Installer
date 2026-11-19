"""Logging with secret masking.

Every secret (token, passwords, generated values) is registered with the :class:`Redactor`
the moment it is created or loaded; everything that reaches the screen or the log file goes
through :meth:`Redactor.redact` first.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

MASK = "***"
# Very short strings would mask half of every log line; real secrets are longer.
MIN_SECRET_LENGTH = 4
LOG_FILE_MODE = 0o640


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, secret: str | None) -> None:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        # Longest first so a secret that contains another one is masked as a whole.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, MASK)
        return text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Logger:
    """Appends masked lines to ``<directory>/YYYY-MM-DD.log``.

    With ``directory=None`` nothing is written and the lines are kept in ``records`` (tests).
    """

    def __init__(
        self,
        directory: Path | None,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._directory = directory
        self.redactor = redactor or Redactor()
        self._clock = clock
        self.records: list[str] = []

    def _write(self, level: str, message: str) -> None:
        now = self._clock()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        prefix = f"{stamp} {level:<5}"
        lines = [f"{prefix} {self.redactor.redact(line)}" for line in message.splitlines()]
        if not lines:
            lines = [prefix]
        self.records.extend(lines)
        if self._directory is None:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{now.strftime('%Y-%m-%d')}.log"
        # Created with its final mode from the start; never chmod'ed afterwards.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def output(self, text: str) -> None:
        """Command output: masked like everything else."""
        self._write("OUT", text)
