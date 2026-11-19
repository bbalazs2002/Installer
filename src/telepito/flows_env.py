"""Menu flow 3: passwords and ``.env`` (specification 5.3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .app import Context
from .envconfig import EnvConfigError, check_value
from .envfile import read_env
from .envmanage import describe_env, edit_value, rotate_secret
from .errors import TelepitoError
from .manifest import Manifest
from .state import InstallRecord
from .ui.base import Item, UiBack, ask_validated
from .vault import Vault, VaultError, validate_master_password

_BACK_NOTE = "A módosítás az alkalmazás újraindítása után lép életbe."
DB_ENTRY = "__database__"


def secrets_menu(ctx: Context) -> None:
    actions = {
        "env": env_screen,
        "rotate": rotate_screen,
        "vault": vault_menu,
    }
    default = "env"
    while True:
        try:
            choice = ctx.ui.menu(
                "Jelszavak és .env",
                [
                    Item("env", "A .env átnézése / szerkesztése"),
                    Item("rotate", "Jelszó generálása / beírása / forgatása"),
                    Item("vault", "Jelszótár"),
                ],
                default=default,
            )
        except UiBack:
            return
        default = choice
        from .app import run_action

        run_action(ctx, actions[choice])


# ----------------------------------------------------------------------------- A) .env


def env_screen(ctx: Context) -> None:
    record = ctx.pick_install("A .env átnézése")
    if record is None:
        return
    info, manifest, layout = ctx.load_install(record)
    default: str | None = None
    while True:
        values = read_env(layout.env_file)
        rows = describe_env(manifest, values)
        items = [Item(r.name, f"{r.name} = {r.display}") for r in rows]
        if not items:
            ctx.ui.message("Nincs beállított változó.", title=".env")
            return
        name = ctx.ui.menu(
            f"{record.name} — .env",
            items,
            text="Titkos értékek nem láthatók. Válassz egy változót a módosításhoz.",
            default=default,
        )
        default = name
        row = next(r for r in rows if r.name == name)
        if not row.editable:
            ctx.ui.message(
                f"A(z) {name} nem módosítható itt "
                "(rögzített érték, adatbázis-hivatkozás vagy ismeretlen változó).",
                title=name,
            )
            continue
        if row.secret:
            rotate_secret(ctx.ui, manifest, layout.env_file, name, ctx.log.redactor, ctx.vault)
        else:
            item = manifest.env_by_name(name)
            current = values.get(name, item.default if item and item.default else "")
            new = ask_validated(
                ctx.ui,
                f"{name} új értéke:",
                _validator_for(manifest, name),
                title=name,
                default=current,
            )
            edit_value(layout.env_file, manifest, name, new, ctx.log.redactor)
        ctx.log.info(f"{record.name}: {name} módosítva.")
        _offer_restart(ctx, record, note=_BACK_NOTE)


def _validator_for(manifest: Manifest, name: str) -> Callable[[str], str | None]:
    def validate(value: str) -> str | None:
        return _edit_error(manifest, name, value)

    return validate


def _edit_error(manifest: Manifest, name: str, value: str) -> str | None:
    item = manifest.env_by_name(name)
    return check_value(item, value) if item is not None else "Ismeretlen változó."


# --------------------------------------------------------------------------- B) rotate


def rotate_screen(ctx: Context) -> None:
    record = ctx.pick_install("Jelszó forgatása")
    if record is None:
        return
    info, manifest, layout = ctx.load_install(record)
    items = [Item(i.name, i.name, i.description[:60]) for i in manifest.env if i.secret]
    if manifest.database is not None and info.database is not None:
        items.append(Item(DB_ENTRY, "Adatbázis-jelszó", "ALTER ROLE + DATABASE_URL egy lépésben"))
    if not items:
        ctx.ui.message("Ennek az alkalmazásnak nincs titkos változója.", title="Jelszavak")
        return
    choice = ctx.ui.menu("Melyik jelszót cseréled?", items)
    if choice == DB_ENTRY:
        assert info.database is not None
        ctx.database_ops().rotate_password(manifest, info.database, layout.env_file)
    else:
        rotate_secret(ctx.ui, manifest, layout.env_file, choice, ctx.log.redactor, ctx.vault)
    ctx.log.info(f"{record.name}: {'adatbázis-jelszó' if choice == DB_ENTRY else choice} cserélve.")
    _offer_restart(ctx, record, note=_BACK_NOTE)


def _offer_restart(ctx: Context, record: InstallRecord, *, note: str) -> None:
    if not ctx.ui.confirm(
        f"{note}\n\nÚjraindítod most az alkalmazást?", title="Újraindítás", default=True
    ):
        return
    problem = ctx.installer().restart(Path(record.path))
    if problem is None:
        ctx.ui.message("Az alkalmazás újraindult és egészséges.", title="Újraindítás")
    else:
        ctx.ui.message(
            f"Az újraindítás után az alkalmazás nem egészséges:\n{problem}", title="Hiba"
        )


# ---------------------------------------------------------------------------- C) vault


def vault_menu(ctx: Context) -> None:
    actions = {
        "list": _vault_list,
        "export": _vault_export,
        "import": _vault_import,
        "password": _vault_password,
    }
    while True:
        try:
            choice = ctx.ui.menu(
                "Jelszótár",
                [
                    Item("list", "A tárolt nevek listája"),
                    Item("export", "Exportálás (titkosított másolat)"),
                    Item("import", "Importálás másik jelszótárból"),
                    Item("password", "A mester-jelszó cseréje"),
                ],
            )
        except UiBack:
            return
        from .app import run_action

        run_action(ctx, actions[choice])


def _unlocked(ctx: Context) -> Vault:
    vault = ctx.vault()
    if vault is None:
        raise TelepitoError("Jelszótár nélkül ez a művelet nem végezhető el.")
    return vault


def _vault_list(ctx: Context) -> None:
    vault = _unlocked(ctx)
    if not vault.installations():
        ctx.ui.message("A jelszótár üres.", title="Jelszótár")
        return
    lines: list[str] = []
    for installation in vault.installations():
        lines.append(installation)
        lines += [f"  {name}" for name in vault.names(installation)]
    ctx.ui.message("\n".join(lines) + "\n\n(Az értékek nem jelennek meg.)", title="Jelszótár")


def _vault_export(ctx: Context) -> None:
    vault = _unlocked(ctx)
    default = str(Path.home() / "telepito-vault-export.enc")
    text = ctx.ui.text(
        "Hová exportáljam? (titkosított fájl lesz)", title="Exportálás", default=default
    )
    destination = Path(text.strip())
    if destination.exists() and not ctx.ui.confirm(f"A(z) {destination} már létezik. Felülírod?"):
        return
    vault.export_to(destination)
    ctx.ui.message(f"Kész: {destination}", title="Exportálás")


def _vault_import(ctx: Context) -> None:
    vault = _unlocked(ctx)
    source = Path(ctx.ui.text("Az importálandó jelszótár fájlja:", title="Importálás").strip())
    password = ctx.ui.secret("A fájl mester-jelszava:", title="Importálás")
    try:
        other = Vault.open(source, password)
    except VaultError as exc:
        raise TelepitoError(str(exc)) from exc
    conflicts = vault.merge_from(other)
    if conflicts:
        listing = "\n".join(f"  {inst}/{name}" for inst, name in conflicts)
        if ctx.ui.confirm(
            "Ezeknek már van más értékük a jelszótárban:\n"
            f"{listing}\n\nFelülírod őket az importáltakkal?",
            title="Importálás",
        ):
            vault.merge_from(other, overwrite=True)
    vault.save()
    other.register_secrets(ctx.log.redactor)
    ctx.ui.message("Az importálás kész.", title="Importálás")


def _vault_password(ctx: Context) -> None:
    vault = _unlocked(ctx)
    while True:
        first = ask_validated(
            ctx.ui,
            "Új mester-jelszó (legalább 12 karakter):",
            validate_master_password,
            title="Mester-jelszó",
            secret=True,
        )
        if ctx.ui.secret("Még egyszer:", title="Mester-jelszó") == first:
            break
        ctx.ui.message("A két jelszó nem egyezik.", title="Mester-jelszó")
    vault.change_password(first)
    ctx.ui.message("A mester-jelszó megváltozott.", title="Mester-jelszó")


__all__ = ["EnvConfigError", "env_screen", "rotate_screen", "secrets_menu", "vault_menu"]
