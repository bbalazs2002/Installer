"""Validation of the target directory typed by the user (specification 5.1, step 3)."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .state import InstallRecord


@dataclass(frozen=True)
class TargetCheck:
    path: Path | None  # the resolved (realpath) directory when acceptable
    error: str | None = None


def _inside(child: str, parent: str) -> bool:
    child_n, parent_n = os.path.normcase(child), os.path.normcase(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


def sudo_hint(path: str) -> str:
    return (
        "Hozd létre root jogosultsággal, majd próbáld újra:\n"
        f"  sudo mkdir -p {path} && sudo chown $USER {path} && sudo chmod 2775 {path}"
    )


def validate_target(
    text: str,
    *,
    state_dir: Path,
    registry: Sequence[InstallRecord],
    own_path: str | None = None,
) -> TargetCheck:
    """Check a user-supplied installation directory.

    Rules: absolute; not ``/``; not the installer's own directory; not inside (or around)
    another installation; the *real* path (symlinks resolved) must satisfy the same rules; it
    must exist and be writable. ``own_path`` is the directory of the installation being
    updated, which is of course allowed.
    """
    raw = text.strip()
    if not raw:
        return TargetCheck(None, "Adj meg egy mappát.")
    if not os.path.isabs(raw):
        return TargetCheck(None, "Abszolút útvonal kell (a / jellel kezdődjön).")
    real = os.path.realpath(raw)
    anchor = os.path.realpath(os.sep)
    if os.path.normcase(real) == os.path.normcase(anchor):
        return TargetCheck(None, "A gyökérmappa nem lehet célmappa.")

    own = os.path.realpath(own_path) if own_path else None
    state_real = os.path.realpath(state_dir)
    if _inside(real, state_real) or _inside(state_real, real):
        return TargetCheck(None, "A telepítő saját mappája nem lehet célmappa.")
    for record in registry:
        other = os.path.realpath(record.path)
        if own is not None and os.path.normcase(other) == os.path.normcase(own):
            continue
        if _inside(real, other) or _inside(other, real):
            return TargetCheck(
                None, f"Ütközik egy másik telepítéssel ({record.name}: {record.path})."
            )

    if not os.path.exists(real):
        return TargetCheck(None, f"A mappa nem létezik: {raw}\n{sudo_hint(raw)}")
    if not os.path.isdir(real):
        return TargetCheck(None, "Ez nem mappa.")
    if not os.access(real, os.W_OK | os.X_OK):
        return TargetCheck(None, f"A mappa nem írható: {raw}\n{sudo_hint(raw)}")
    return TargetCheck(Path(real))
