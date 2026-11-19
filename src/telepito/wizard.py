"""The interactive ``.env.production`` wizard (specification 8.1) and the vault dialogs.

Rules: ``fixed`` values are never asked; ``advanced`` variables are skipped (their defaults are
used) unless the user opens them; secrets can be generated, typed (hidden, twice) or taken from
the vault, and are never displayed. Generated or typed secrets are saved to the vault once all
questions are answered, so cancelling half way leaves nothing behind.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from .envconfig import DefaultEnvConfigurator, EnvChanges, EnvConfigurator, check_value
from .envfile import generate_secret
from .logging_ import Redactor
from .manifest import EnvVar, Manifest
from .ui.base import Item, Ui, ask_validated
from .vault import (
    Vault,
    VaultAuthError,
    VaultError,
    unlock,
    validate_master_password,
)

VaultProvider = Callable[[], Vault | None]

_FORGOTTEN_WARNING = (
    "A mester-jelszó sehol nincs eltárolva. Ha elfelejted, a jelszótár tartalma NEM állítható "
    "helyre."
)


def open_vault_interactive(
    ui: Ui,
    path: Path,
    redactor: Redactor,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Vault | None:
    """Unlock the vault (or create it on first use). ``None`` if the user declines to create it.

    Esc raises :class:`~telepito.ui.base.UiBack` like everywhere else.
    """
    if Vault.exists(path):

        def ask(attempt: int) -> str:
            if attempt > 1:
                ui.message("Hibás jelszó vagy sérült jelszótár.", title="Jelszótár")
            return ui.secret("A jelszótár mester-jelszava:", title="Jelszótár")

        vault = unlock(path, ask, sleep=sleep)
        vault.register_secrets(redactor)
        return vault

    if not ui.confirm(
        "Még nincs jelszótár. Létrehozod most?\n\n" + _FORGOTTEN_WARNING,
        title="Jelszótár",
        default=True,
    ):
        return None
    while True:
        first = ask_validated(
            ui,
            "Új mester-jelszó (legalább 12 karakter):",
            validate_master_password,
            title="Jelszótár",
            secret=True,
        )
        if ui.secret("Mester-jelszó még egyszer:", title="Jelszótár") == first:
            break
        ui.message("A két jelszó nem egyezik.", title="Jelszótár")
    path.parent.mkdir(parents=True, exist_ok=True)
    return Vault.create(path, first)


class InteractiveEnvConfigurator(EnvConfigurator):
    def __init__(
        self, ui: Ui, redactor: Redactor, vault_provider: VaultProvider | None = None
    ) -> None:
        self._ui = ui
        self._redactor = redactor
        self._vault_provider = vault_provider
        self._vault: Vault | None = None
        self._vault_asked = False

    # ------------------------------------------------------------------- vault

    def _get_vault(self) -> Vault | None:
        if not self._vault_asked and self._vault_provider is not None:
            self._vault_asked = True
            try:
                self._vault = self._vault_provider()
            except VaultError as exc:
                self._ui.message(f"A jelszótár nem használható: {exc}", title="Jelszótár")
                self._vault = None
        return self._vault

    # ---------------------------------------------------------------- the wizard

    def configure(
        self,
        manifest: Manifest,
        existing: Mapping[str, str],
        *,
        is_new: bool,
        previous_names: Collection[str] = (),
    ) -> EnvChanges:
        values: dict[str, str] = {}
        to_vault: dict[str, str] = {}

        pending = [i for i in manifest.env if i.fixed is None and i.name not in existing]
        for item in manifest.env:
            if item.secret and item.name in existing:
                self._redactor.register(existing[item.name])
            if item.fixed is not None:
                values[item.name] = item.fixed

        basic = [i for i in pending if i.group != "advanced"]
        advanced = [i for i in pending if i.group == "advanced"]
        for item in basic:
            self._decide(manifest, item, values, to_vault)

        # The advanced group comes last, behind one question; unopened, it keeps its defaults.
        open_advanced = bool(advanced) and self._ui.confirm(
            "Megnyitod a speciális beállításokat?\n\n" + "\n".join(f"  {i.name}" for i in advanced),
            title="Speciális beállítások",
        )
        defaults = DefaultEnvConfigurator(self._redactor)
        for item in advanced:
            if open_advanced:
                self._decide(manifest, item, values, to_vault)
            else:
                values.update(defaults.configure(_only(manifest, item), {}, is_new=True).set)

        self.save_to_vault(manifest.name, to_vault)
        return EnvChanges(values, self._removals(manifest, existing, previous_names))

    def _decide(
        self, manifest: Manifest, item: EnvVar, values: dict[str, str], to_vault: dict[str, str]
    ) -> None:
        value = self.ask_secret(manifest, item, to_vault) if item.secret else self._ask(item)
        if value is None:
            return
        if item.secret:
            self._redactor.register(value)
        values[item.name] = value

    def _ask(self, item: EnvVar) -> str | None:
        """A plain variable: Enter takes the default."""
        title = item.name
        hint = f"\n\n({item.name}" + (f", alapérték: {item.default})" if item.default else ")")
        prompt = (item.description or item.name) + hint

        def validator(answer: str) -> str | None:
            if answer == "":
                if item.default is not None or not item.required:
                    return None
                return "Ez a mező kötelező."
            return check_value(item, answer)

        answer = ask_validated(self._ui, prompt, validator, title=title, default=item.default or "")
        if answer == "":
            return item.default
        return answer

    def ask_secret(
        self,
        manifest: Manifest,
        item: EnvVar,
        to_vault: dict[str, str],
        *,
        allow_stored: bool = True,
    ) -> str | None:
        """Ask how to obtain a secret's value. Newly created values are added to ``to_vault``."""
        ui = self._ui
        vault = self._get_vault()
        stored = vault.get(manifest.name, item.name) if vault is not None and allow_stored else None
        options: list[Item] = []
        if item.generate_bytes is not None:
            options.append(Item("generate", "Generálás (véletlen érték)"))
        options.append(Item("manual", "Kézi beírás (rejtett)"))
        if stored is not None:
            options.append(Item("vault", "A jelszótárban lévő érték használata"))
        if not item.required:
            options.append(Item("skip", "Üresen hagyom"))
        text = item.description or item.name
        if item.rotation_warning:
            text += f"\n\n{item.rotation_warning}"
        choice = ui.menu(item.name, options, text=text)

        if choice == "skip":
            return None
        if choice == "vault":
            assert stored is not None
            error = check_value(item, stored)
            if error is not None:
                ui.message(f"A tárolt érték nem használható: {error}", title=item.name)
                return self.ask_secret(manifest, item, to_vault, allow_stored=allow_stored)
            return stored
        if choice == "generate":
            assert item.generate_bytes is not None
            value = generate_secret(item.generate_bytes)
        else:
            value = self._ask_manual(item)
        to_vault[item.name] = value
        return value

    def _ask_manual(self, item: EnvVar) -> str:
        ui = self._ui
        while True:
            first = ask_validated(
                ui,
                f"{item.name} értéke (rejtett):",
                lambda v: check_value(item, v),
                title=item.name,
                secret=True,
            )
            if ui.secret("Még egyszer, megerősítésként:", title=item.name) == first:
                return first
            ui.message("A két érték nem egyezik.", title=item.name)

    def save_to_vault(self, installation: str, secrets_: dict[str, str]) -> None:
        if not secrets_:
            return
        vault = self._get_vault()
        if vault is None:
            self._ui.message(
                "A titkok nem kerültek jelszótárba, csak a .env.production tartalmazza őket. "
                "Készíts róluk saját biztonsági másolatot.",
                title="Jelszótár",
            )
            return
        try:
            for name, value in secrets_.items():
                vault.set(installation, name, value)
            vault.save()
        except VaultError as exc:
            self._ui.message(
                f"A titkokat nem sikerült a jelszótárba menteni ({exc}). "
                "Csak a .env.production tartalmazza őket.",
                title="Jelszótár",
            )

    def _removals(
        self, manifest: Manifest, existing: Mapping[str, str], previous_names: Collection[str]
    ) -> tuple[str, ...]:
        declared = {i.name for i in manifest.env}
        if manifest.database is not None:
            declared.add(manifest.database.url_env)
        removed = [n for n in previous_names if n not in declared and n in existing]
        if not removed:
            return ()
        listing = "\n".join(f"  {name}" for name in removed)
        if self._ui.confirm(
            f"Az új kiadás már nem használja ezeket a változókat:\n\n{listing}\n\nTörlöd őket?",
            title="Eltávolított változók",
        ):
            return tuple(removed)
        return ()


def _only(manifest: Manifest, item: EnvVar) -> Manifest:
    """A copy of the manifest containing just ``item`` (to reuse the default logic)."""
    from dataclasses import replace

    return replace(manifest, env=(item,), database=None)


__all__ = [
    "InteractiveEnvConfigurator",
    "VaultAuthError",
    "VaultProvider",
    "open_vault_interactive",
]
