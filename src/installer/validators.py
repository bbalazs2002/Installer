"""Pure validators for identifiers that come from users or from release metadata."""

from __future__ import annotations

import re

REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
DB_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A plain file name: no directory separators, no parent references.
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


VOLUME_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_HOST_PATH_SEGMENTS = 4


def is_volume_name(value: str) -> bool:
    """The name a ``deploy.json`` gives a data volume (it appears in backup file names)."""
    return VOLUME_NAME_RE.match(value) is not None


def is_container_path(value: str) -> bool:
    """An absolute POSIX path without ``..``, quotes, colons or control characters."""
    if not value.startswith("/") or value == "/":
        return False
    if any(ord(c) < 32 or c in "\"'`:\\\x7f" for c in value):
        return False
    return all(part not in ("..", ".") for part in value.split("/")[1:] if part != "")


def is_relative_data_path(value: str) -> bool:
    """A relative sub-path below the data directory: plain segments, no ``..``."""
    parts = value.split("/")
    return len(parts) <= _MAX_HOST_PATH_SEGMENTS and all(
        _PATH_SEGMENT_RE.match(p) is not None and ".." not in p for p in parts
    )


def is_repo(value: str) -> bool:
    """``owner/repo`` as used by GitHub (rejects ``.`` and ``..`` segments)."""
    if not REPO_RE.match(value):
        return False
    return all(part not in (".", "..") for part in value.split("/"))


def is_app_name(value: str) -> bool:
    return APP_NAME_RE.match(value) is not None


def is_db_identifier(value: str) -> bool:
    return DB_IDENT_RE.match(value) is not None


def is_env_name(value: str) -> bool:
    return ENV_NAME_RE.match(value) is not None


def is_asset_name(value: str) -> bool:
    return ASSET_NAME_RE.match(value) is not None and ".." not in value
