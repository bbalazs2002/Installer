"""Menu flow 3: passwords and ``.env`` (specification 5.3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .app import Context
from .envconfig import EnvConfigError, check_value
from .envfile import read_env
from .envmanage import describe_env, edit_value, rotate_secret
from .errors import InstallerError
from .manifest import Manifest
from .state import InstallRecord
from .ui.base import Item, UiBack, ask_validated
from .vault import Vault, VaultError, validate_master_password

_BACK_NOTE = "The change takes effect after the application is restarted."
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
                "Passwords and .env",
                [
                    Item("env", "View / edit the .env"),
                    Item("rotate", "Generate / enter / rotate a password"),
                    Item("vault", "Vault"),
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
    record = ctx.pick_install("View the .env")
    if record is None:
        return
    info, manifest, layout = ctx.load_install(record)
    default: str | None = None
    while True:
        values = read_env(layout.env_file)
        rows = describe_env(manifest, values)
        items = [Item(r.name, f"{r.name} = {r.display}") for r in rows]
        if not items:
            ctx.ui.message("There are no variables set.", title=".env")
            return
        name = ctx.ui.menu(
            f"{record.name} — .env",
            items,
            text="Secret values are not shown. Pick a variable to change it.",
            default=default,
        )
        default = name
        row = next(r for r in rows if r.name == name)
        if not row.editable:
            ctx.ui.message(
                f"{name} cannot be changed here "
                "(fixed value, database reference or unknown variable).",
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
                f"New value of {name}:",
                _validator_for(manifest, name),
                title=name,
                default=current,
            )
            edit_value(layout.env_file, manifest, name, new, ctx.log.redactor)
        ctx.log.info(f"{record.name}: {name} changed.")
        _offer_restart(ctx, record, note=_BACK_NOTE)


def _validator_for(manifest: Manifest, name: str) -> Callable[[str], str | None]:
    def validate(value: str) -> str | None:
        return _edit_error(manifest, name, value)

    return validate


def _edit_error(manifest: Manifest, name: str, value: str) -> str | None:
    item = manifest.env_by_name(name)
    return check_value(item, value) if item is not None else "Unknown variable."


# --------------------------------------------------------------------------- B) rotate


def rotate_screen(ctx: Context) -> None:
    record = ctx.pick_install("Rotate a password")
    if record is None:
        return
    info, manifest, layout = ctx.load_install(record)
    items = [Item(i.name, i.name, i.description[:60]) for i in manifest.env if i.secret]
    if manifest.database is not None and info.database is not None:
        items.append(Item(DB_ENTRY, "Database password", "ALTER ROLE + DATABASE_URL in one step"))
    if not items:
        ctx.ui.message("This application has no secret variables.", title="Passwords")
        return
    choice = ctx.ui.menu("Which password do you want to change?", items)
    if choice == DB_ENTRY:
        assert info.database is not None
        ctx.database_ops().rotate_password(manifest, info.database, layout.env_file)
    else:
        rotate_secret(ctx.ui, manifest, layout.env_file, choice, ctx.log.redactor, ctx.vault)
    ctx.log.info(f"{record.name}: {'database password' if choice == DB_ENTRY else choice} rotated.")
    _offer_restart(ctx, record, note=_BACK_NOTE)


def _offer_restart(ctx: Context, record: InstallRecord, *, note: str) -> None:
    if not ctx.ui.confirm(f"{note}\n\nRestart the application now?", title="Restart", default=True):
        return
    problem = ctx.installer().restart(Path(record.path))
    if problem is None:
        ctx.ui.message("The application restarted and is healthy.", title="Restart")
    else:
        ctx.ui.message(
            f"The application is not healthy after the restart:\n{problem}", title="Error"
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
                "Vault",
                [
                    Item("list", "List the stored names"),
                    Item("export", "Export (encrypted copy)"),
                    Item("import", "Import from another vault"),
                    Item("password", "Change the master password"),
                ],
            )
        except UiBack:
            return
        from .app import run_action

        run_action(ctx, actions[choice])


def _unlocked(ctx: Context) -> Vault:
    vault = ctx.vault()
    if vault is None:
        raise InstallerError("This operation is not possible without a vault.")
    return vault


def _vault_list(ctx: Context) -> None:
    vault = _unlocked(ctx)
    if not vault.installations():
        ctx.ui.message("The vault is empty.", title="Vault")
        return
    lines: list[str] = []
    for installation in vault.installations():
        lines.append(installation)
        lines += [f"  {name}" for name in vault.names(installation)]
    ctx.ui.message("\n".join(lines) + "\n\n(Values are not shown.)", title="Vault")


def _vault_export(ctx: Context) -> None:
    vault = _unlocked(ctx)
    default = str(Path.home() / "installer-vault-export.enc")
    text = ctx.ui.text(
        "Where should I export it? (it will be an encrypted file)", title="Export", default=default
    )
    destination = Path(text.strip())
    if destination.exists() and not ctx.ui.confirm(f"{destination} already exists. Overwrite it?"):
        return
    vault.export_to(destination)
    ctx.ui.message(f"Done: {destination}", title="Export")


def _vault_import(ctx: Context) -> None:
    vault = _unlocked(ctx)
    source = Path(ctx.ui.text("The vault file to import:", title="Import").strip())
    password = ctx.ui.secret("The master password of that file:", title="Import")
    try:
        other = Vault.open(source, password)
    except VaultError as exc:
        raise InstallerError(str(exc)) from exc
    conflicts = vault.merge_from(other)
    if conflicts:
        listing = "\n".join(f"  {inst}/{name}" for inst, name in conflicts)
        if ctx.ui.confirm(
            "These already have a different value in the vault:\n"
            f"{listing}\n\nOverwrite them with the imported ones?",
            title="Import",
        ):
            vault.merge_from(other, overwrite=True)
    vault.save()
    other.register_secrets(ctx.log.redactor)
    ctx.ui.message("The import is done.", title="Import")


def _vault_password(ctx: Context) -> None:
    vault = _unlocked(ctx)
    while True:
        first = ask_validated(
            ctx.ui,
            "New master password (at least 12 characters):",
            validate_master_password,
            title="Master password",
            secret=True,
        )
        if ctx.ui.secret("Once more:", title="Master password") == first:
            break
        ctx.ui.message("The two passwords do not match.", title="Master password")
    vault.change_password(first)
    ctx.ui.message("The master password was changed.", title="Master password")


__all__ = ["EnvConfigError", "env_screen", "rotate_screen", "secrets_menu", "vault_menu"]
