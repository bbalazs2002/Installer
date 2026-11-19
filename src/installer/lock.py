"""A non-blocking, cross-platform exclusive file lock (``flock`` on Linux)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType

from .errors import TelepitoError

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockBusy(TelepitoError):
    """Another installer instance holds the lock."""


class FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("The lock is already held.")
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o640)
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LockBusy("Another installer is running. Wait until it finishes.") from exc
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
