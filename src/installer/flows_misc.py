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
        ctx.log.warning(f"Checking for updates failed: {exc}")
        return 0


def updates_screen(ctx: Context) -> None:
    records = ctx.registry()
    if not records:
        ctx.ui.message("There are no installed applications yet.", title="Available updates")
        return
    infos = find_updates(ctx.github(), records)
    now = ctx.clock()
    available = [i for i in infos if i.available]
    if not available:
        ctx.ui.message(
            "\n".join(format_update_line(i, now) for i in infos) + "\n\nEverything is up to date.",
            title="Available updates",
        )
        return
    by_name = {i.record.name: i for i in available}
    choice = ctx.ui.menu(
        "Available updates",
        [Item(i.record.name, format_update_line(i, now)) for i in available],
        text="Pick one to start the update.",
    )
    info = by_name[choice]
    assert info.latest is not None
    install_flow(ctx, Preset(info.record.repo, info.latest.tag, info.record.path))


# ------------------------------------------------------------------- 4: rollback


def rollback_screen(ctx: Context) -> None:
    record = ctx.pick_install("Roll back to a previous version")
    if record is None:
        return
    installer = ctx.installer()
    versions = installer.previous_versions(Path(record.path))
    if not versions:
        ctx.ui.message(
            "There is no previous version to roll back to "
            "(both the release directory and the Docker image are needed).",
            title="Roll back",
        )
        return
    chosen = ctx.ui.choose(
        f"{record.name}: which version do you want to roll back to?",
        [Item(v, v) for v in versions],
        default=versions[0],
    )
    result = installer.rollback_to(Path(record.path), chosen)
    ctx.ui.message(f"Version {result.version} is running.", title="Roll back")


# ------------------------------------------------------------------- 6/7: status, logs


def status_screen(ctx: Context) -> None:
    records = ctx.registry()
    with ctx.ui.progress("Status", "Querying the applications...") as bar:
        bar.update(10)
        statuses = collect_status(records, ctx.docker, ctx.health)
        bar.update(100)
    parts = [format_status(statuses), "", _token_line(ctx), "", "Latest log lines:"]
    parts += _recent_log_lines(ctx) or ["  (no log)"]
    ctx.ui.message("\n".join(parts), title="Status", scroll=True)


def _token_line(ctx: Context) -> str:
    if ctx.tokens.load() is None:
        return "GitHub token: not set"
    repos = [r.repo for r in ctx.registry()] or [k.repo for k in ctx.known_repos()]
    if not repos:
        return "GitHub token: set (its expiry cannot be checked, there is no known repository)"
    try:
        status = ctx.github().check_access(repos[0])
    except TelepitoError as exc:
        return f"GitHub-token: {exc}"
    if status.expires_at is None:
        return "GitHub token: valid (no expiry date)"
    warning = token_expiry_warning(status, ctx.clock())
    when = status.expires_at.strftime("%Y-%m-%d")
    return f"GitHub token: expires {when}" + (f"  ! {warning}" if warning else "")


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
        ctx.ui.message("There are no logs yet.", title="Logs")
        return
    by_name = {f.name: f for f in files}
    default: str | None = None
    while True:
        name = ctx.ui.menu("Logs", [Item(f.name, f.name) for f in files], default=default)
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
                "Settings",
                [
                    Item("token", "Set and verify the GitHub token"),
                    Item("repos", "Known repositories"),
                ],
            )
        except UiBack:
            return
        run_action(ctx, actions[choice])


def _set_token(ctx: Context) -> None:
    ui = ctx.ui
    ui.message(
        "A fine-grained personal access token is needed, for only the required repositories:\n"
        "  Contents: Read, Metadata: Read\n"
        "Expiration: at most 90 days.",
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
            "A repository the token must be able to access (owner/repo):",
            lambda v: None if is_repo(v.strip()) else "The format is owner/repo.",
            title="GitHub-token",
        ).strip()
    status = client.check_access(repo)
    ctx.tokens.save(token)
    warning = token_expiry_warning(status, ctx.clock())
    when = status.expires_at.strftime("%Y-%m-%d") if status.expires_at else "not given"
    ui.message(
        f"The token is valid and can access {repo}.\nExpires: {when}"
        + (f"\n\n{warning}" if warning else ""),
        title="GitHub-token",
    )


def _manage_repos(ctx: Context) -> None:
    ui = ctx.ui
    while True:
        repos = ctx.known_repos()
        items = [Item("__add__", "Add a repository...")]
        items += [Item(k.repo, k.repo, k.default_path or "") for k in repos]
        try:
            choice = ui.menu("Known repositories", items)
        except UiBack:
            return
        if choice == "__add__":
            repo = ask_validated(
                ui,
                "The repository name (owner/repo):",
                lambda v: None if is_repo(v.strip()) else "The format is owner/repo.",
                title="New repository",
            ).strip()
            if any(k.repo == repo for k in repos):
                ui.message("This repository is already on the list.", title="Known repositories")
                continue
            path = ui.text(
                "Suggested target directory (may be left empty):", title=repo, default=""
            ).strip()
            save_repos(ctx.paths.repos_file, [*repos, KnownRepo(repo, path or None)])
            continue
        if ui.confirm(f"Remove {choice} from the list?", title="Known repositories"):
            save_repos(ctx.paths.repos_file, [k for k in repos if k.repo != choice])


# ------------------------------------------------------------------- 9: self-update


def selfupdate_screen(ctx: Context) -> None:
    from . import __version__
    from .app import ExitRequested
    from .selfupdate import SELF_REPO, apply_update, find_update

    ui = ctx.ui
    if ctx.pyz_path is None:
        ui.message(
            "This instance is not running from telepito.pyz (development start), "
            "so it cannot be updated.",
            title="Update the installer",
        )
        return
    github = ctx.github()
    release = find_update(github, __version__)
    if release is None:
        ui.message(
            f"The installer is up to date (telepito {__version__}).", title="Update the installer"
        )
        return
    if not ui.confirm(
        f"A new installer is available: {__version__} → {release.version}\n\n"
        f"Source: {SELF_REPO}, release {release.tag}\n"
        "I will download it, verify the sum, try it out, and replace telepito.pyz "
        "(the old file is kept as .previous).",
        title="Update the installer",
        default=True,
    ):
        return
    with ui.progress("Update the installer", f"Downloading {release.tag}...") as bar:
        bar.update(10)
        result = apply_update(github, release, ctx.pyz_path, ctx.log)
        bar.update(100)
    ui.message(
        f"The installer was updated: {result.old_version} → {result.new_version}.\n"
        f"The old file: {result.previous_file}\n\n"
        "The installer will now exit; start it again to use the new version.",
        title="Update the installer",
    )
    raise ExitRequested
