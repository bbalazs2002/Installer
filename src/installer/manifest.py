"""Parsing and validation of ``deploy.json`` (schemaVersion 1). Pure logic, no I/O."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import InstallerError
from .validators import (
    is_app_name,
    is_asset_name,
    is_db_identifier,
    is_env_name,
)

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_KEEP = 3
URL_TEMPLATE_FIELDS = frozenset({"user", "password", "host", "port", "database"})
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
GROUPS = ("basic", "advanced")

_ROOT_KEYS = {
    "schemaVersion", "name", "displayName", "image", "compose", "port", "health",
    "database", "migrations", "env", "proxy", "postInstall", "keep",
}  # fmt: skip
_ENV_KEYS = {
    "name", "description", "default", "fixed", "required", "secret", "generate",
    "validate", "minLength", "rotationWarning", "group",
}  # fmt: skip
_DB_KEYS = {
    "required", "engine", "description", "urlEnv", "urlTemplate",
    "defaultDatabase", "defaultUser",
}  # fmt: skip


class ManifestError(InstallerError):
    """The ``deploy.json`` is invalid or unsupported. The message is user-facing."""


@dataclass(frozen=True)
class EnvVar:
    name: str
    description: str = ""
    default: str | None = None
    fixed: str | None = None
    required: bool = False
    secret: bool = False
    generate_bytes: int | None = None
    validate: str | None = None
    min_length: int | None = None
    rotation_warning: str | None = None
    group: str = "basic"


@dataclass(frozen=True)
class DatabaseSpec:
    engine: str
    description: str
    url_env: str
    url_template: str
    default_database: str
    default_user: str


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    name: str
    display_name: str
    image_name: str
    compose_file: str
    project_name: str
    port_default: int
    port_env: str
    health_path: str
    health_timeout_seconds: int
    health_version_field: str
    # ``None`` when the application needs no database (missing or ``required: false``).
    database: DatabaseSpec | None = None
    migrations_asset: str | None = None
    env: tuple[EnvVar, ...] = ()
    proxy_note: str = ""
    proxy_snippet: str = ""
    post_install: tuple[str, ...] = ()
    keep: int = DEFAULT_KEEP
    # Non-fatal findings (e.g. unknown keys), already user-facing.
    warnings: tuple[str, ...] = field(default=(), compare=False)

    def proxy_snippet_for(self, port: int) -> str:
        """The Apache snippet with ``{port}`` substituted (plain replace; braces stay safe)."""
        return self.proxy_snippet.replace("{port}", str(port))

    def env_by_name(self, name: str) -> EnvVar | None:
        for item in self.env:
            if item.name == name:
                return item
        return None


class _Reader:
    """Typed access to JSON values that reports precise paths and collects warnings."""

    def __init__(self, warnings: list[str]) -> None:
        self.warnings = warnings

    @staticmethod
    def obj(value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ManifestError(f"The field {path} must be an object.")
        return value

    def known(self, data: dict[str, Any], allowed: set[str], path: str) -> None:
        for key in data:
            if key not in allowed:
                where = f"{path}.{key}" if path else key
                self.warnings.append(f"Unknown key in deploy.json: {where} (ignored)")

    @staticmethod
    def required(data: dict[str, Any], key: str, path: str) -> Any:
        if key not in data:
            where = f"{path}.{key}" if path else key
            raise ManifestError(f"Required field missing in deploy.json: {where}")
        return data[key]

    @staticmethod
    def string(value: Any, path: str, *, allow_empty: bool = True) -> str:
        if not isinstance(value, str):
            raise ManifestError(f"The field {path} must be a string.")
        if not allow_empty and not value:
            raise ManifestError(f"The field {path} must not be empty.")
        return value

    @staticmethod
    def integer(value: Any, path: str, *, minimum: int, maximum: int | None = None) -> int:
        # bool is a subclass of int; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(f"The field {path} must be an integer.")
        if value < minimum or (maximum is not None and value > maximum):
            limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise ManifestError(f"The value of {path} is invalid (must be {limit}).")
        return value

    @staticmethod
    def boolean(value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            raise ManifestError(f"The field {path} must be true or false.")
        return value


def _parse_env_item(reader: _Reader, raw: Any, index: int) -> EnvVar:
    path = f"env[{index}]"
    data = reader.obj(raw, path)
    reader.known(data, _ENV_KEYS, path)
    name = reader.string(reader.required(data, "name", path), f"{path}.name", allow_empty=False)
    if not is_env_name(name):
        raise ManifestError(f"Invalid environment variable name: {name!r}")
    path = f"env[{name}]"

    description = reader.string(data.get("description", ""), f"{path}.description")
    default = data.get("default")
    fixed = data.get("fixed")
    if default is not None:
        default = reader.string(default, f"{path}.default")
    if fixed is not None:
        fixed = reader.string(fixed, f"{path}.fixed")
    if default is not None and fixed is not None:
        raise ManifestError(f"{path} cannot have both default and fixed.")

    required = reader.boolean(data.get("required", False), f"{path}.required")
    secret = reader.boolean(data.get("secret", False), f"{path}.secret")
    if secret and fixed is not None:
        raise ManifestError(f"{path} is secret and cannot have a fixed value.")

    generate_bytes: int | None = None
    if "generate" in data:
        generate = reader.obj(data["generate"], f"{path}.generate")
        reader.known(generate, {"bytes"}, f"{path}.generate")
        generate_bytes = reader.integer(
            reader.required(generate, "bytes", f"{path}.generate"),
            f"{path}.generate.bytes",
            minimum=1,
            maximum=1024,
        )
        if not secret:
            raise ManifestError(f"{path}: generate can only be used with a secret element.")

    validate = data.get("validate")
    if validate is not None:
        validate = reader.string(validate, f"{path}.validate", allow_empty=False)
        try:
            compiled = re.compile(validate)
        except re.error as exc:
            raise ManifestError(
                f"{path}.validate is not a valid regular expression: {exc}"
            ) from exc
        for label, candidate in (("default", default), ("fixed", fixed)):
            if candidate is not None and compiled.fullmatch(candidate) is None:
                raise ManifestError(f"{path}.{label} does not match its own validate pattern.")

    min_length: int | None = None
    if "minLength" in data:
        min_length = reader.integer(data["minLength"], f"{path}.minLength", minimum=0)

    rotation_warning = data.get("rotationWarning")
    if rotation_warning is not None:
        rotation_warning = reader.string(rotation_warning, f"{path}.rotationWarning")
        if not secret:
            reader.warnings.append(
                f"{path}.rotationWarning only applies to secret elements (ignored)"
            )
            rotation_warning = None

    group = reader.string(data.get("group", "basic"), f"{path}.group")
    if group not in GROUPS:
        raise ManifestError(f"{path}.group must be 'basic' or 'advanced', not {group!r}.")

    return EnvVar(
        name=name,
        description=description,
        default=default,
        fixed=fixed,
        required=required,
        secret=secret,
        generate_bytes=generate_bytes,
        validate=validate,
        min_length=min_length,
        rotation_warning=rotation_warning,
        group=group,
    )


def _parse_database(reader: _Reader, raw: Any, app_name: str) -> DatabaseSpec | None:
    data = reader.obj(raw, "database")
    reader.known(data, _DB_KEYS, "database")
    if not reader.boolean(data.get("required", False), "database.required"):
        return None
    engine = reader.string(data.get("engine", "postgres"), "database.engine")
    if engine != "postgres":
        raise ManifestError(f"Unsupported database engine: {engine!r} (only postgres for now).")
    url_env = reader.string(
        reader.required(data, "urlEnv", "database"), "database.urlEnv", allow_empty=False
    )
    if not is_env_name(url_env):
        raise ManifestError(f"Invalid database.urlEnv: {url_env!r}")
    template = reader.string(
        reader.required(data, "urlTemplate", "database"), "database.urlTemplate", allow_empty=False
    )
    unknown = set(_PLACEHOLDER_RE.findall(template)) - URL_TEMPLATE_FIELDS
    if unknown:
        raise ManifestError(
            "database.urlTemplate contains an unknown placeholder: "
            + ", ".join(sorted("{" + u + "}" for u in unknown))
        )
    fallback = app_name.replace("-", "_")
    default_db = reader.string(data.get("defaultDatabase", fallback), "database.defaultDatabase")
    default_user = reader.string(data.get("defaultUser", fallback), "database.defaultUser")
    for label, value in (("defaultDatabase", default_db), ("defaultUser", default_user)):
        if not is_db_identifier(value):
            raise ManifestError(f"database.{label} is not a valid identifier: {value!r}")
    return DatabaseSpec(
        engine=engine,
        description=reader.string(data.get("description", ""), "database.description"),
        url_env=url_env,
        url_template=template,
        default_database=default_db,
        default_user=default_user,
    )


def parse_manifest(data: Any) -> Manifest:
    """Validate a decoded ``deploy.json`` and return a :class:`Manifest`."""
    warnings: list[str] = []
    reader = _Reader(warnings)
    root = reader.obj(data, "deploy.json")

    version = reader.integer(reader.required(root, "schemaVersion", ""), "schemaVersion", minimum=1)
    if version > SUPPORTED_SCHEMA_VERSION:
        raise ManifestError(
            f"The schemaVersion of deploy.json ({version}) is newer than this installer "
            f"supports ({SUPPORTED_SCHEMA_VERSION}). Update the installer."
        )

    reader.known(root, _ROOT_KEYS, "")

    name = reader.string(reader.required(root, "name", ""), "name", allow_empty=False)
    if not is_app_name(name):
        raise ManifestError(
            f"Invalid application name: {name!r} (lowercase letters, digits, hyphens)."
        )
    display_name = reader.string(root.get("displayName", name), "displayName") or name

    image = reader.obj(reader.required(root, "image", ""), "image")
    reader.known(image, {"name"}, "image")
    image_name = reader.string(
        reader.required(image, "name", "image"), "image.name", allow_empty=False
    )
    if not is_app_name(image_name):
        raise ManifestError(f"Invalid image.name: {image_name!r}")

    compose = reader.obj(reader.required(root, "compose", ""), "compose")
    reader.known(compose, {"file", "projectName"}, "compose")
    compose_file = reader.string(compose.get("file", "docker-compose.deploy.yml"), "compose.file")
    if not is_asset_name(compose_file):
        raise ManifestError(f"Invalid compose.file (must be a plain file name): {compose_file!r}")
    project_name = reader.string(compose.get("projectName", name), "compose.projectName")
    if not is_app_name(project_name):
        raise ManifestError(f"Invalid compose.projectName: {project_name!r}")

    port = reader.obj(reader.required(root, "port", ""), "port")
    reader.known(port, {"default", "env"}, "port")
    port_default = reader.integer(
        reader.required(port, "default", "port"), "port.default", minimum=1, maximum=65535
    )
    port_env = reader.string(port.get("env", "PORT"), "port.env", allow_empty=False)
    if not is_env_name(port_env):
        raise ManifestError(f"Invalid port.env: {port_env!r}")

    health = reader.obj(reader.required(root, "health", ""), "health")
    reader.known(health, {"path", "timeoutSeconds", "versionField"}, "health")
    health_path = reader.string(
        reader.required(health, "path", "health"), "health.path", allow_empty=False
    )
    if not health_path.startswith("/"):
        raise ManifestError("health.path must start with a slash (/).")
    health_timeout = reader.integer(
        health.get("timeoutSeconds", 120), "health.timeoutSeconds", minimum=1, maximum=3600
    )
    health_version_field = reader.string(
        health.get("versionField", "version"), "health.versionField", allow_empty=False
    )

    database = _parse_database(reader, root["database"], name) if "database" in root else None

    migrations_asset: str | None = None
    if "migrations" in root:
        migrations = reader.obj(root["migrations"], "migrations")
        reader.known(migrations, {"listAsset"}, "migrations")
        if "listAsset" in migrations:
            migrations_asset = reader.string(
                migrations["listAsset"], "migrations.listAsset", allow_empty=False
            )
            if not is_asset_name(migrations_asset):
                raise ManifestError(f"Invalid migrations.listAsset: {migrations_asset!r}")

    raw_env = root.get("env", [])
    if not isinstance(raw_env, list):
        raise ManifestError("The env field must be a list.")
    env = tuple(_parse_env_item(reader, item, i) for i, item in enumerate(raw_env))
    seen: set[str] = set()
    for item in env:
        if item.name in seen:
            raise ManifestError(f"{item.name} appears twice in the env list.")
        seen.add(item.name)
    if database is not None and database.url_env in seen:
        raise ManifestError(
            f"{database.url_env} is the database.urlEnv, so it must not be listed in env."
        )
    if port_env in seen:
        raise ManifestError(f"{port_env} is the port.env, so it must not be listed in env.")

    proxy_note = proxy_snippet = ""
    if "proxy" in root:
        proxy = reader.obj(root["proxy"], "proxy")
        reader.known(proxy, {"note", "snippet"}, "proxy")
        proxy_note = reader.string(proxy.get("note", ""), "proxy.note")
        proxy_snippet = reader.string(proxy.get("snippet", ""), "proxy.snippet")

    raw_post = root.get("postInstall", [])
    if not isinstance(raw_post, list):
        raise ManifestError("The postInstall field must be a list.")
    post_install = tuple(
        reader.string(item, f"postInstall[{i}]") for i, item in enumerate(raw_post)
    )

    keep = reader.integer(root.get("keep", DEFAULT_KEEP), "keep", minimum=1, maximum=100)

    return Manifest(
        schema_version=version,
        name=name,
        display_name=display_name,
        image_name=image_name,
        compose_file=compose_file,
        project_name=project_name,
        port_default=port_default,
        port_env=port_env,
        health_path=health_path,
        health_timeout_seconds=health_timeout,
        health_version_field=health_version_field,
        database=database,
        migrations_asset=migrations_asset,
        env=env,
        proxy_note=proxy_note,
        proxy_snippet=proxy_snippet,
        post_install=post_install,
        keep=keep,
        warnings=tuple(warnings),
    )


def load_manifest(raw: bytes | str) -> Manifest:
    """Decode JSON text/bytes and validate it."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError(f"deploy.json is not valid JSON: {exc}") from exc
    return parse_manifest(data)
