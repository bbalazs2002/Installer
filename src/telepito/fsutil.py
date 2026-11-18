"""Atomic file writes. Files holding secrets are created with the final mode up front."""

from __future__ import annotations

import contextlib
import os
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
