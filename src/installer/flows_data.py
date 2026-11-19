"""Menu flow 5: backups of the database and the data volumes (specification 5.5)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .app import Context, run_action
from .backup import (
    KIND_DATABASE,
    BackupFile,
    BackupSet,
    group_sets,
    list_all_backups,
    offer_retention,
)
from .errors import InstallerError
from .installer import InstalledData, RestoreChoice
from .state import save_install_info
from .ui.base import Item, UiBack


def backup_menu(ctx: Context) -> None:
    record = ctx.pick_install("Backups", with_data=True)
    if record is None:
        return
    while True:
        try:
            choice = ctx.ui.menu(
                f"Backups — {record.name}",
                [
                    Item("backup", "Back up now (database and data volumes)"),
                    Item("restore", "Restore from a backup"),
                    Item("cleanup", "Delete old backups"),
                ],
            )
        except UiBack:
            return
        action = {"backup": _backup_now, "restore": _restore, "cleanup": _cleanup}[choice]
        run_action(ctx, lambda c, a=action: a(c, record.path, record.name))  # type: ignore[misc]


def _data(ctx: Context, path: str) -> InstalledData:
    data = ctx.installer().installed_data(Path(path))
    if data.info.database is None and not data.volumes:
        raise InstallerError("This installation has no database and no data volumes.")
    return data


def _database_name(data: InstalledData) -> str | None:
    return data.info.database.name if data.info.database is not None else None


def set_key(backup_set: BackupSet) -> str:
    return f"{backup_set.created}|{backup_set.version}"


def _backup_now(ctx: Context, path: str, name: str) -> None:
    data = _data(ctx, path)
    wanted = [v for v in data.volumes if v.spec.backup]
    stop_app = False
    if wanted:
        stop_app = ctx.ui.confirm(
            "Stop the application while the volumes are copied?\n"
            "Recommended: the copy is then consistent. The application is unavailable for the "
            "time the copy takes and is started again afterwards.",
            title="Backup",
            default=True,
        )
    dump, archives, problem = ctx.installer().backup_now(Path(path), stop_app=stop_app)
    if dump is None and not archives:
        ctx.ui.message("You continued without a backup; none was made.", title="Backup")
        return
    created = [p for p in (dump, *archives) if p is not None]
    ctx.log.info(f"{name}: manual backup: " + ", ".join(p.name for p in created))
    lines = ["The backup was created:"] + [str(p) for p in created]
    if problem is not None:
        lines += ["", f"The application is not healthy after starting again:\n{problem}"]
    ctx.ui.message("\n".join(lines), title="Backup")
    _retention(ctx, path, force=False)


def _restore(ctx: Context, path: str, name: str) -> None:
    data = _data(ctx, path)
    layout = data.layout
    sets = group_sets(list_all_backups(layout.backups, _database_name(data)))
    if not sets:
        ctx.ui.message("There is no backup of this installation.", title="Restore")
        return
    by_key = {set_key(s): s for s in sets}
    items = [Item(set_key(s), s.label()) for s in sets]
    chosen = by_key[
        ctx.ui.choose("Which backup should I restore from?", items, default=items[0].key)
    ]

    parts = {f.series: f for f in chosen.files}
    if len(parts) > 1:
        picked = set(
            ctx.ui.choose_many(
                "What should be restored?",
                [Item(series, _part_label(parts[series]), selected=True) for series in parts],
                text="Each part asks for its own confirmation and saves the current state first.",
            )
        )
    else:
        picked = set(parts)
    if not picked:
        ctx.ui.message("Nothing was selected.", title="Restore")
        return

    dump = next((parts[s].path for s in picked if parts[s].kind == KIND_DATABASE), None)
    volumes = tuple(
        (parts[s].database, parts[s].path) for s in sorted(picked) if parts[s].kind != KIND_DATABASE
    )
    problem = ctx.installer().restore_data(Path(path), RestoreChoice(dump, volumes))
    ctx.log.info(f"{name}: restored from the backup set {set_key(chosen)}")
    if problem is None:
        ctx.ui.message("The data was restored and the application restarted.", title="Restore")
    else:
        ctx.ui.message(
            f"The data was restored, but the application is not healthy:\n{problem}",
            title="Restore",
        )


def _part_label(part: BackupFile) -> str:
    kind = "database" if part.kind == KIND_DATABASE else "volume"
    return f"{kind} {part.database} ({part.size / 2**20:.1f} MB)"


def _cleanup(ctx: Context, path: str, name: str) -> None:
    _retention(ctx, path, force=True)


def _retention(ctx: Context, path: str, *, force: bool) -> None:
    data = _data(ctx, path)
    layout, info = data.layout, data.info
    if not list_all_backups(layout.backups, _database_name(data)):
        ctx.ui.message("There are no backups.", title="Backups")
        return
    flag = offer_retention(
        ctx.ui,
        ctx.log,
        layout.backups,
        auto_delete=info.auto_delete_old_backups,
        now=ctx.clock(),
        force_prompt=force,
        database=_database_name(data),
    )
    if flag != info.auto_delete_old_backups:
        save_install_info(layout.root, replace(info, auto_delete_old_backups=flag))
    elif force:
        ctx.ui.message("Done.", title="Backups")
