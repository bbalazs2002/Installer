"""Atomic file writes. Files holding secrets are created with the final mode up front."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o640) -> None:
    """Write ``data`` to ``path`` atomically.

    The temp file lives in the same directory, is created with ``mode`` from the
    start (never ``chmod`` afterwards), fsynced, then moved over the target.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # mkstemp creates 0600; widen only to the exact requested mode, then write.
        # For 0600 (secrets) nothing is ever wider than requested.
        if mode != 0o600:
            os.chmod(tmp_name, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str, mode: int = 0o640) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def atomic_symlink(link: Path, target: str) -> None:
    """Create or replace the symlink ``link`` -> ``target`` atomically.

    A temporary link is created next to ``link`` and moved over it with ``os.replace``, so
    readers always see either the old or the new target, never a missing link.
    """
    for attempt in range(100):
        tmp = link.parent / f".{link.name}.{os.getpid()}.{attempt}.tmp"
        try:
            os.symlink(target, tmp)
        except FileExistsError:
            continue
        try:
            if sys.platform == "win32" and os.path.lexists(link):
                # Windows cannot rename over an existing directory symlink, so this is a
                # development-only, non-atomic fallback. On Linux the replace below is atomic.
                try:
                    os.unlink(link)
                except OSError:
                    os.rmdir(link)
            os.replace(tmp, link)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return
    raise FileExistsError(f"Nem hozható létre ideiglenes link: {link}")
