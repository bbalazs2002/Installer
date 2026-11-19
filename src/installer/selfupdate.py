"""Updating the installer itself (specification 5.8, 12).

The installer ships as one file, ``installer.pyz``, attached to a release of its own repository
together with a ``SHA256SUMS``. An update downloads both, verifies the checksum, *runs* the new
file once (``--version``) to make sure it works on this Python and is the version it claims to
be, keeps the old file as ``installer.pyz.previous``, and only then replaces the running file
atomically. A failed step at any point leaves the installed file untouched.

Releases also carry the same archive as ``telepito.pyz`` for installers of the 0.1.x generation
(see :mod:`installer.identity`); a copy running from a file with that name renames itself to
``installer.pyz`` on first start (:func:`migrate_legacy_name`).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .errors import InstallerError
from .fsutil import atomic_write_bytes
from .github import GitHub, Release
from .identity import LEGACY_PYZ_NAME, PROGRAM, PYZ_NAME
from .installer import ReleaseError, parse_sums, verify_file
from .logging_ import Logger
from .versions import Version

SELF_REPO = "bbalazs2002/Installer"
PYZ_ASSET = PYZ_NAME
SUMS_ASSET = "SHA256SUMS"
PREVIOUS_SUFFIX = ".previous"
DEFAULT_MODE = 0o750
CHECK_TIMEOUT = 30


class SelfUpdateError(InstallerError):
    """The update cannot be applied; the installed file is unchanged."""


@dataclass(frozen=True)
class SelfUpdateResult:
    old_version: str
    new_version: str
    previous_file: Path


def running_pyz() -> Path | None:
    """The ``.pyz`` file this process runs from, or ``None`` (e.g. a source checkout)."""
    candidate = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.suffix == ".pyz" and resolved.is_file() else None


def find_update(github: GitHub, current: str = __version__) -> Release | None:
    """The newest stable release of the installer, if it is newer than ``current``."""
    release = github.latest_stable(SELF_REPO)
    if release is None or release.version is None:
        return None
    running = Version.try_parse(current)
    if running is not None and release.version <= running:
        return None
    return release


def apply_update(github: GitHub, release: Release, pyz_path: Path, log: Logger) -> SelfUpdateResult:
    """Download, verify, test and install ``release`` over ``pyz_path``."""
    version = release.version
    if version is None:
        raise SelfUpdateError(f"The tag {release.tag} is not a valid version.")
    if pyz_path.suffix != ".pyz" or not pyz_path.is_file():
        raise SelfUpdateError(
            "Only an installer running from installer.pyz can be updated (this one is not)."
        )
    pyz_asset, sums_asset = release.asset(PYZ_ASSET), release.asset(SUMS_ASSET)
    if pyz_asset is None or sums_asset is None:
        raise SelfUpdateError(f"The release is missing {PYZ_ASSET} or {SUMS_ASSET}.")

    directory = pyz_path.parent
    try:
        mode = stat.S_IMODE(pyz_path.stat().st_mode) or DEFAULT_MODE
        scratch = Path(tempfile.mkdtemp(prefix=".installer-update-", dir=directory))
    except OSError as exc:
        raise SelfUpdateError(
            f"Cannot write to {directory} ({exc}); the update is not possible."
        ) from exc

    try:
        sums_path, new_path = scratch / SUMS_ASSET, scratch / PYZ_ASSET
        log.info(f"Self-update: {__version__} -> {version} ({release.tag})")
        github.download_asset(SELF_REPO, sums_asset, sums_path)
        github.download_asset(SELF_REPO, pyz_asset, new_path)
        sums = parse_sums(sums_path.read_text(encoding="utf-8"))
        expected = sums.get(PYZ_ASSET)
        if expected is None:
            raise SelfUpdateError(f"{SUMS_ASSET} does not contain the sum of {PYZ_ASSET}.")
        try:
            verify_file(new_path, expected, PYZ_ASSET)
        except ReleaseError as exc:
            raise SelfUpdateError(str(exc)) from exc
        _smoke_test(new_path, version)

        previous = pyz_path.with_name(pyz_path.name + PREVIOUS_SUFFIX)
        atomic_write_bytes(previous, pyz_path.read_bytes(), mode)
        os.chmod(new_path, mode)
        os.replace(new_path, pyz_path)
        log.info(f"The installer was updated to {version}. The old file: {previous.name}")
        return SelfUpdateResult(__version__, str(version), previous)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _smoke_test(candidate: Path, expected: Version) -> None:
    """Run the downloaded file once: it must start here and report the expected version."""
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(candidate), "--version"],
            capture_output=True,
            timeout=CHECK_TIMEOUT,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelfUpdateError(f"The new installer cannot be started ({exc}).") from exc
    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:]
        raise SelfUpdateError(
            "The new installer does not run on this Python" + (f": {detail[0]}" if detail else ".")
        )
    if output != f"{PROGRAM} {expected}":
        raise SelfUpdateError(
            f"The new installer reports a different version ({output!r}) than the release ({expected})."
        )


def migrate_legacy_name(pyz: Path, log: Logger) -> Path:
    """Rename a file still called ``telepito.pyz`` to ``installer.pyz``.

    ``telepito.pyz`` is left behind as a symlink to the new name, so shortcuts that point at the
    old name keep working. Nothing is overwritten and any failure leaves everything as it was.
    Returns the (possibly new) path of the running file.
    """
    if pyz.name != LEGACY_PYZ_NAME:
        return pyz
    target = pyz.with_name(PYZ_NAME)
    if target.exists() or target.is_symlink():
        return pyz
    try:
        os.replace(pyz, target)
    except OSError as exc:
        log.warning(f"Renaming {pyz.name} to {PYZ_NAME} failed: {exc}")
        return pyz
    try:
        os.symlink(PYZ_NAME, pyz)  # relative link, valid wherever the directory is moved to
    except OSError as exc:
        os.replace(target, pyz)  # without the link the old name would vanish: undo the rename
        log.warning(f"Creating the {pyz.name} link failed, the rename was undone: {exc}")
        return pyz
    old_previous = pyz.with_name(pyz.name + PREVIOUS_SUFFIX)
    new_previous = target.with_name(target.name + PREVIOUS_SUFFIX)
    if old_previous.is_file() and not new_previous.exists():
        try:
            os.replace(old_previous, new_previous)
        except OSError:
            pass  # cosmetic only
    log.info(f"Renamed {pyz.name} to {PYZ_NAME} ({pyz.name} is now a link to it).")
    return target


__all__ = [
    "SELF_REPO",
    "SelfUpdateError",
    "SelfUpdateResult",
    "apply_update",
    "find_update",
    "migrate_legacy_name",
    "running_pyz",
]
