"""The smaller menu flows: updates, rollback, status, logs, settings, self-update."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from .app import Context
from .errors import TelepitoError
from .flows_install import Preset, install_flow
from .github_http import token_expiry_warning
from .state import KnownRepo, save_repos
from .status import collect_status, format_status
from .ui.base import Item, UiBack, ask_validated
from .updates import find_updates, format_update_line
from .validators import is_repo

LOG_TAIL_LINES = 20
LOG_VIEW_BYTES = 300_000


# ------------------------------------------------------------------- 2: updates


def pending_updates(ctx: Context) -> int:
    """How many installed applications have a newer stable release (0 on any problem)."""
    if ctx.tokens.load() is None:
        return 0
    try:
        records = ctx.registry()
        return sum(1 for u in find_updates(ctx.github(), records) if u.available)
    except TelepitoError as exc:
        ctx.log.warning(f"A frissítések ellenőrzése nem sikerült: {exc}")
        return 0


def updates_screen(ctx: Context) -> None:
    records = ctx.registry()
    if not records:
        ctx.ui.message("Még nincs telepített alkalmazás.", title="Elérhető frissítések")
        return
    infos = find_updates(ctx.github(), records)
    now = ctx.clock()
    available = [i for i in infos if i.available]
    if not available:
        ctx.ui.message(
            "\n".join(format_update_line(i, now) for i in infos) + "\n\nMinden naprakész.",
            title="Elérhető frissítések",
        )
        return
    by_name = {i.record.name: i for i in available}
    choice = ctx.ui.menu(
        "Elérhető frissítések",
        [Item(i.record.name, format_update_line(i, now)) for i in available],
        text="Válassz egyet a frissítés indításához.",
    )
    info = by_name[choice]
    assert info.latest is not None
    install_flow(ctx, Preset(info.record.repo, info.latest.tag, info.record.path))


# ------------------------------------------------------------------- 4: rollback


def rollback_screen(ctx: Context) -> None:
    record = ctx.pick_install("Visszaállítás előző verzióra")
    if record is None:
        return
    installer = ctx.installer()
    versions = installer.previous_versions(Path(record.path))
    if not versions:
        ctx.ui.message(
            "Nincs visszaállítható korábbi verzió "
            "(a kiadás mappája és a Docker-kép is kell hozzá).",
            title="Visszaállítás",
        )
        return
    chosen = ctx.ui.choose(
        f"{record.name}: melyik verzióra állsz vissza?",
        [Item(v, v) for v in versions],
        default=versions[0],
    )
    result = installer.rollback_to(Path(record.path), chosen)
    ctx.ui.message(f"A(z) {result.version} verzió fut.", title="Visszaállítás")


# ------------------------------------------------------------------- 6/7: status, logs


def status_screen(ctx: Context) -> None:
    records = ctx.registry()
    with ctx.ui.progress("Állapot", "Az alkalmazások lekérdezése...") as bar:
        bar.update(10)
        statuses = collect_status(records, ctx.docker, ctx.health)
        bar.update(100)
    parts = [format_status(statuses), "", _token_line(ctx), "", "Az utolsó naplósorok:"]
    parts += _recent_log_lines(ctx) or ["  (nincs napló)"]
    ctx.ui.message("\n".join(parts), title="Állapot", scroll=True)


def _token_line(ctx: Context) -> str:
    if ctx.tokens.load() is None:
        return "GitHub-token: nincs megadva"
    repos = [r.repo for r in ctx.registry()] or [k.repo for k in ctx.known_repos()]
    if not repos:
        return "GitHub-token: megadva (lejárata nem ellenőrizhető, nincs ismert repó)"
    try:
        status = ctx.github().check_access(repos[0])
    except TelepitoError as exc:
        return f"GitHub-token: {exc}"
    if status.expires_at is None:
        return "GitHub-token: érvényes (nincs lejárati dátum)"
    warning = token_expiry_warning(status, ctx.clock())
    when = status.expires_at.strftime("%Y-%m-%d")
    return f"GitHub-token: lejár {when}" + (f"  ! {warning}" if warning else "")


def _log_files(ctx: Context) -> list[Path]:
    try:
        return sorted(ctx.paths.logs_dir.glob("*.log"), reverse=True)
    except OSError:
        return []


def _recent_log_lines(ctx: Context) -> list[str]:
    lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
    for path in reversed(_log_files(ctx)[:2]):  # the two newest, oldest of them first
        try:
            lines.extend(f"  {line}" for line in path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
    return list(lines)


def logs_screen(ctx: Context) -> None:
    files = _log_files(ctx)
    if not files:
        ctx.ui.message("Még nincs napló.", title="Naplók")
        return
    by_name = {f.name: f for f in files}
    default: str | None = None
    while True:
        name = ctx.ui.menu("Naplók", [Item(f.name, f.name) for f in files], default=default)
        default = name
        data = by_name[name].read_bytes()[-LOG_VIEW_BYTES:]
        ctx.ui.message(data.decode("utf-8", errors="replace"), title=name, scroll=True)


# ------------------------------------------------------------------- 8: settings


def settings_menu(ctx: Context) -> None:
    from .app import run_action

    actions = {"token": _set_token, "repos": _manage_repos}
    while True:
        try:
            choice = ctx.ui.menu(
                "Beállítások",
                [
                    Item("token", "GitHub-token megadása és ellenőrzése"),
                    Item("repos", "Ismert repók"),
                ],
            )
        except UiBack:
            return
        run_action(ctx, actions[choice])


def _set_token(ctx: Context) -> None:
    ui = ctx.ui
    ui.message(
        "Fine-grained personal access token kell, csak a szükséges repókra:\n"
        "  Contents: Read, Metadata: Read\n"
        "Lejárat: legfeljebb 90 nap.",
        title="GitHub-token",
    )
    token = ui.secret("A token:", title="GitHub-token").strip()
    ctx.log.redactor.register(token)
    client = ctx.github_factory(token)  # rejects malformed tokens up front
    repos = [k.repo for k in ctx.known_repos()] or [r.repo for r in ctx.registry()]
    if repos:
        repo = repos[0]
    else:
        repo = ask_validated(
            ui,
            "Egy repó, amelyhez a tokennek hozzá kell férnie (owner/repo):",
            lambda v: None if is_repo(v.strip()) else "A formátum: owner/repo.",
            title="GitHub-token",
        ).strip()
    status = client.check_access(repo)
    ctx.tokens.save(token)
    warning = token_expiry_warning(status, ctx.clock())
    when = status.expires_at.strftime("%Y-%m-%d") if status.expires_at else "nincs megadva"
    ui.message(
        f"A token érvényes, és hozzáfér a(z) {repo} repóhoz.\nLejárat: {when}"
        + (f"\n\n{warning}" if warning else ""),
        title="GitHub-token",
    )


def _manage_repos(ctx: Context) -> None:
    ui = ctx.ui
    while True:
        repos = ctx.known_repos()
        items = [Item("__add__", "Új repó felvétele...")]
        items += [Item(k.repo, k.repo, k.default_path or "") for k in repos]
        try:
            choice = ui.menu("Ismert repók", items)
        except UiBack:
            return
        if choice == "__add__":
            repo = ask_validated(
                ui,
                "A repó neve (owner/repo):",
                lambda v: None if is_repo(v.strip()) else "A formátum: owner/repo.",
                title="Új repó",
            ).strip()
            if any(k.repo == repo for k in repos):
                ui.message("Ez a repó már szerepel a listán.", title="Ismert repók")
                continue
            path = ui.text(
                "Javasolt célmappa (üresen is hagyható):", title=repo, default=""
            ).strip()
            save_repos(ctx.paths.repos_file, [*repos, KnownRepo(repo, path or None)])
            continue
        if ui.confirm(f"Eltávolítod a(z) {choice} repót a listáról?", title="Ismert repók"):
            save_repos(ctx.paths.repos_file, [k for k in repos if k.repo != choice])


# ------------------------------------------------------------------- 9: self-update


def selfupdate_screen(ctx: Context) -> None:
    from . import __version__
    from .app import ExitRequested
    from .selfupdate import SELF_REPO, apply_update, find_update

    ui = ctx.ui
    if ctx.pyz_path is None:
        ui.message(
            "Ez a példány nem a telepito.pyz fájlból fut (fejlesztői indítás), "
            "ezért nem frissíthető.",
            title="A telepítő frissítése",
        )
        return
    github = ctx.github()
    release = find_update(github, __version__)
    if release is None:
        ui.message(f"A telepítő naprakész (telepito {__version__}).", title="A telepítő frissítése")
        return
    if not ui.confirm(
        f"Új telepítő érhető el: {__version__} → {release.version}\n\n"
        f"Forrás: {SELF_REPO}, kiadás {release.tag}\n"
        "Letöltöm, ellenőrzöm az összeget, kipróbálom, és kicserélem a telepito.pyz fájlt "
        "(a régi megmarad .previous néven).",
        title="A telepítő frissítése",
        default=True,
    ):
        return
    with ui.progress("A telepítő frissítése", f"{release.tag} letöltése...") as bar:
        bar.update(10)
        result = apply_update(github, release, ctx.pyz_path, ctx.log)
        bar.update(100)
    ui.message(
        f"A telepítő frissítve: {result.old_version} → {result.new_version}.\n"
        f"A régi fájl: {result.previous_file}\n\n"
        "A telepítő most kilép; indítsd újra az új verzióval.",
        title="A telepítő frissítése",
    )
    raise ExitRequested
