"""Deciding the values of the application's environment variables.

:class:`EnvConfigurator` is the extension point used by the installer; the interactive wizard
(T.3) implements it too. :class:`DefaultEnvConfigurator` is the non-interactive variant: fixed
values and defaults, generated secrets, and an error for a required value it cannot invent.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from .envfile import generate_secret, validate_pair
from .errors import TelepitoError
from .logging_ import Redactor
from .manifest import EnvVar, Manifest


class EnvConfigError(TelepitoError):
    """A variable has no valid value. The message is user-facing."""


@dataclass(frozen=True)
class EnvChanges:
    """What to write into ``.env.production``: values to set and names to delete."""

    set: Mapping[str, str] = field(default_factory=dict)
    remove: tuple[str, ...] = ()


class EnvConfigurator(ABC):
    @abstractmethod
    def configure(
        self,
        manifest: Manifest,
        existing: Mapping[str, str],
        *,
        is_new: bool,
        previous_names: Collection[str] = (),
    ) -> EnvChanges:
        """Decide the values.

        ``existing`` is the current ``.env.production`` (empty for a new installation). For an
        update only variables missing from ``existing`` are decided. ``previous_names`` are the
        variables the installed release declared; those no longer declared are returned in
        ``remove``.
        """


def check_value(item: EnvVar, value: str) -> str | None:
    """Return an error message if ``value`` is not acceptable for ``item``, else ``None``."""
    if item.min_length is not None and len(value) < item.min_length:
        return f"A(z) {item.name} legalább {item.min_length} karakter kell legyen."
    if item.validate is not None and re.fullmatch(item.validate, value) is None:
        return f"A(z) {item.name} értéke nem felel meg a megadott formátumnak."
    try:
        validate_pair(item.name, value)
    except TelepitoError as exc:
        return str(exc)
    return None


class DefaultEnvConfigurator(EnvConfigurator):
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def configure(
        self,
        manifest: Manifest,
        existing: Mapping[str, str],
        *,
        is_new: bool,
        previous_names: Collection[str] = (),
    ) -> EnvChanges:
        values: dict[str, str] = {}
        for item in manifest.env:
            if item.secret and item.name in existing:
                self._redactor.register(existing[item.name])
            if item.fixed is not None:
                values[item.name] = item.fixed
                continue
            if item.name in existing:
                continue
            value = self._decide(item)
            if value is None:
                continue
            error = check_value(item, value)
            if error is not None:
                raise EnvConfigError(error)
            if item.secret:
                self._redactor.register(value)
            values[item.name] = value

        declared = {item.name for item in manifest.env}
        if manifest.database is not None:
            declared.add(manifest.database.url_env)
        removed = tuple(
            name for name in previous_names if name not in declared and name in existing
        )
        return EnvChanges(values, removed)

    @staticmethod
    def _decide(item: EnvVar) -> str | None:
        if item.default is not None:
            return item.default
        if item.secret and item.generate_bytes is not None:
            return generate_secret(item.generate_bytes)
        if item.required:
            raise EnvConfigError(
                f"A(z) {item.name} kötelező, de nincs alapértéke, és nem generálható."
            )
        return None
