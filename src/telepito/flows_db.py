"""Menu flow 5: database backup, restore and cleanup (specification 5.5)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .app import Context, run_action
from .backup import list_backups, offer_retention
from .docker import DockerError
from .errors import TelepitoError
from .state import save_install_info
from .ui.base import Item, UiBack


def database_menu(ctx: Context) -> None:
    record = ctx.pick_install("Adatbázis", with_database=True)
    if record is None:
        return
    while True:
        try:
            choice = ctx.ui.menu(
                f"Adatbázis — {record.name}",
                [
                    Item("backup", "Mentés most"),
                    Item("restore", "Visszatöltés mentésből"),
                    Item("cleanup", "Régi mentések törlése"),
                ],
            )
        except UiBack:
            return
        action = {"backup": _backup_now, "restore": _restore, "cleanup": _cleanup}[choice]
        run_action(ctx, lambda c, a=action: a(c, record.path, record.name))  # type: ignore[misc]


def _context(ctx: Context, path: str):  # type: ignore[no-untyped-def]
    from .state import InstallRecord

    record = InstallRecord("", "", path, "", "")
    info, manifest, layout = ctx.load_install(record)
    if info.database is None:
        raise TelepitoError("Ennek a telepítésnek nincs adatbázisa.")
    return info, manifest, layout


def _backup_now(ctx: Context, path: str, name: str) -> None:
    info, _, layout = _context(ctx, path)
    assert info.database is not None
    dump = ctx.database_ops().backup(info.database, info.version, layout.backups)
    if dump is None:
        ctx.ui.message("Mentés nélkül folytattad; nem készült mentés.", title="Mentés")
        return
    ctx.log.info(f"{name}: kézi adatbázis-mentés: {dump.name}")
    ctx.ui.message(f"A mentés elkészült:\n{dump}", title="Mentés")
    _retention(ctx, path, force=False)


def _restore(ctx: Context, path: str, name: str) -> None:
    info, manifest, layout = _context(ctx, path)
    assert info.database is not None
    files = list_backups(layout.backups, info.database.name)
    if not files:
        ctx.ui.message("Nincs mentés ehhez az adatbázishoz.", title="Visszatöltés")
        return
    by_path = {str(f.path): f for f in files}
    items = [
        Item(
            str(f.path),
            f"{f.version:<12} {f.created}  {f.size / 2**20:.1f} MB".replace(".", ","),
        )
        for f in files
    ]
    chosen = by_path[ctx.ui.choose("Melyik mentésből töltsem vissza?", items, default=items[0].key)]

    release_dir = layout.release_dir(info.version)
    compose_file = release_dir / manifest.compose_file

    def stop_application() -> None:
        try:
            ctx.docker.compose_stop(manifest.project_name, compose_file, layout.compose_env)
        except DockerError as exc:
            raise TelepitoError(f"Az alkalmazást nem sikerült leállítani: {exc}") from exc

    ctx.database_ops().restore(info.database, chosen.path, prepare=stop_application)
    ctx.log.info(f"{name}: adatbázis visszatöltve: {chosen.path.name}")
    problem = ctx.installer().restart(Path(path))
    if problem is None:
        ctx.ui.message("Az adatbázis visszatöltve, az alkalmazás újraindult.", title="Visszatöltés")
    else:
        ctx.ui.message(
            f"Az adatbázis visszatöltve, de az alkalmazás nem egészséges:\n{problem}",
            title="Visszatöltés",
        )


def _cleanup(ctx: Context, path: str, name: str) -> None:
    _retention(ctx, path, force=True)


def _retention(ctx: Context, path: str, *, force: bool) -> None:
    info, _, layout = _context(ctx, path)
    assert info.database is not None
    if not list_backups(layout.backups, info.database.name):
        ctx.ui.message("Nincs mentés.", title="Mentések")
        return
    flag = offer_retention(
        ctx.ui,
        ctx.log,
        layout.backups,
        auto_delete=info.auto_delete_old_backups,
        now=ctx.clock(),
        force_prompt=force,
        database=info.database.name,
    )
    if flag != info.auto_delete_old_backups:
        save_install_info(layout.root, replace(info, auto_delete_old_backups=flag))
    elif force:
        ctx.ui.message("Kész.", title="Mentések")
