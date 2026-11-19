"""Reading and writing ``.env.production`` and ``compose.env`` (specification 6.7).

Values are written as ``KEY='value'``: single quotes stop Compose from interpreting ``$`` and
``\\``. A value therefore must not contain a single quote, a line break or a NUL byte; such
values are rejected rather than escaped. When an existing file is modified, order, comments and
lines the installer does not manage are left untouched.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path

from .errors import InstallerError
from .fsutil import atomic_write_text
from .validators import is_env_name

SECRET_FILE_MODE = 0o600
_FORBIDDEN = ("'", "\n", "\r", "\x00")


class EnvFileError(InstallerError):
    """A key or value cannot be stored safely."""


def validate_pair(key: str, value: str) -> None:
    if not is_env_name(key):
        raise EnvFileError(f"Invalid environment variable name: {key!r}")
    for char in _FORBIDDEN:
        if char in value:
            raise EnvFileError(
                f"The value of {key} must not contain an apostrophe, a line break or a NUL character."
            )


def render_assignment(key: str, value: str) -> str:
    validate_pair(key, value)
    return f"{key}='{value}'"


def generate_secret(num_bytes: int) -> str:
    """A random secret from the alphabet ``[A-Za-z0-9_-]`` (never needs quoting)."""
    return secrets.token_urlsafe(num_bytes)


def _parse_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    if not is_env_name(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is not None:
            result[parsed[0]] = parsed[1]
    return result


def read_env(path: Path) -> dict[str, str]:
    """Read an env file; a missing file means no variables."""
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def update_text(text: str, set_values: Mapping[str, str], remove: Iterable[str] = ()) -> str:
    """Return ``text`` with ``set_values`` applied and ``remove`` deleted (line based)."""
    removals = set(remove)
    pending = dict(set_values)
    for key, value in pending.items():
        validate_pair(key, value)
    out: list[str] = []
    done: set[str] = set()
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            out.append(line)
            continue
        key = parsed[0]
        if key in removals:
            continue
        if key in pending:
            if key not in done:
                out.append(render_assignment(key, pending[key]))
                done.add(key)
            continue  # a duplicate of a managed key is dropped
        out.append(line)
    for key, value in pending.items():
        if key not in done:
            out.append(render_assignment(key, value))
    return "\n".join(out) + "\n" if out else ""


def update_env_file(path: Path, set_values: Mapping[str, str], remove: Iterable[str] = ()) -> None:
    """Apply changes to ``path`` atomically; the file is created with mode 600 from the start."""
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    atomic_write_text(path, update_text(current, set_values, remove), SECRET_FILE_MODE)
