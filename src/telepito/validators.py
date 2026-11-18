"""Pure validators for identifiers that come from users or from release metadata."""

from __future__ import annotations

import re

REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
DB_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A plain file name: no directory separators, no parent references.
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
