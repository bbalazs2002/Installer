"""Viewing, editing and rotating the settings of an installed application (menu 3).

Secrets are never shown: every value that is secret, unknown to the manifest or part of the
database URL is displayed as stars. The interactive parts take the :class:`~telepito.ui.base.Ui`
and reuse the wizard's secret prompts so rotation behaves exactly like first-time setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .envconfig import EnvConfigError, check_value
from .envfile import read_env, update_env_file
from .errors import UserAbort
from .logging_ import Redactor
from .manifest import EnvVar, Manifest
from .ui.base import Ui
from .wizard import InteractiveEnvConfigurator, VaultProvider

MASKED = "********"
NOT_SET = "(nincs beállítva)"


@dataclass(frozen=True)
class EnvRow:
    name: str
    display: str  # safe to show
    secret: bool
    group: str  # "basic", "advanced" or "other" (not declared by the manifest)
    editable: bool


def describe_env(manifest: Manifest, env: dict[str, str]) -> list[EnvRow]:
    """The current settings as displayable rows; nothing secret ever appears in ``display``."""
    rows: list[EnvRow] = []
    declared: set[str] = set()
    for item in manifest.env:
        declared.add(item.name)
        if item.name not in env:
            rows.append(EnvRow(item.name, NOT_SET, item.secret, item.group, item.fixed is None))
            continue
        shown = MASKED if item.secret else env[item.name]
        rows.append(EnvRow(item.name, shown, item.secret, item.group, item.fixed is None))
    if manifest.database is not None:
        url = manifest.database.url_env
        declared.add(url)
        rows.append(EnvRow(url, MASKED if url in env else NOT_SET, True, "basic", False))
    # Variables somebody added by hand: unknown, therefore treated as secret.
    for name in sorted(set(env) - declared):
        rows.append(EnvRow(name, MASKED, True, "other", False))
    return rows


def _declared(manifest: Manifest, name: str) -> EnvVar:
    item = manifest.env_by_name(name)
    if item is None:
        raise EnvConfigError(f"A(z) {name} nem szerepel a kiadás változói között.")
    return item


def edit_value(
    env_path: Path, manifest: Manifest, name: str, value: str, redactor: Redactor
) -> None:
    """Set one declared, non-fixed variable after validating it."""
    item = _declared(manifest, name)
    if item.fixed is not None:
        raise EnvConfigError(f"A(z) {name} rögzített érték, nem módosítható.")
    error = check_value(item, value)
    if error is not None:
        raise EnvConfigError(error)
    if item.secret:
        redactor.register(value)
    update_env_file(env_path, {name: value})


def rotate_secret(
    ui: Ui,
    manifest: Manifest,
    env_path: Path,
    name: str,
    redactor: Redactor,
    vault_provider: VaultProvider | None,
) -> bool:
    """Replace a secret with a new value (generated or typed). Returns ``True`` if changed.

    The manifest's ``rotationWarning`` is shown and the change needs an explicit confirmation.
    The new value goes to ``.env.production`` and to the vault; the application keeps using the
    old value until it is restarted (the caller offers that).
    """
    item = _declared(manifest, name)
    if not item.secret:
        raise EnvConfigError(f"A(z) {name} nem titok, nem forgatható.")

    warning = (
        f"A(z) {name} cseréje után az alkalmazás csak újraindítás után használja az új értéket."
    )
    if item.rotation_warning:
        warning += f"\n\n{item.rotation_warning}"
    if not ui.confirm(warning + "\n\nFolytatod?", title=f"{name} forgatása"):
        raise UserAbort("A forgatás megszakítva.")

    wizard = InteractiveEnvConfigurator(ui, redactor, vault_provider)
    pending: dict[str, str] = {}
    value = wizard.ask_secret(manifest, item, pending, allow_stored=False)
    if value is None:  # "leave empty" is only offered for optional secrets
        update_env_file(env_path, {}, remove=[name])
        return True
    redactor.register(value)
    update_env_file(env_path, {name: value})
    wizard.save_to_vault(manifest.name, pending)
    return True


def current_values(env_path: Path) -> dict[str, str]:
    """The raw current environment (for callers that must not display it)."""
    return read_env(env_path)
