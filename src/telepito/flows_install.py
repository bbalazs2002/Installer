"""Menu flow 1: install or update an application (specification 5.1)."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .app import Context
from .backup import backup_refs
from .errors import TelepitoError, UserAbort
from .github import GitHub, Release
from .installer import InstallJob, InstallResult
from .manifest import Manifest
from .plan import PlanRequest, build_plan, format_plan
from .state import KnownRepo, inspect_target, load_install_info, save_repos
from .targets import validate_target
from .ui.base import Item, UiBack, ask_validated
from .updates import format_age
from .validators import is_repo
from .versions import Version

OTHER_REPO = "__other__"
TOGGLE_PRERELEASES = "__toggle__"


@dataclass(frozen=True)
class Preset:
    """Answers to pre-fill (set by the "available updates" screen)."""

    repo: str
    tag: str
    target: str


def install_flow(ctx: Context, preset: Preset | None = None) -> None:
    ui = ctx.ui
    github = ctx.github()  # fail early (e.g. no token) before asking anything
    repo = preset.repo if preset is not None else _choose_repo(ctx)
    ctx.paths.tmp_dir.mkdir(parents=True, exist_ok=True)

    release = (
        _find_release(github, repo, preset.tag) if preset else _choose_release(ctx, github, repo)
    )
    installer = ctx.installer()

    work_dir = Path(tempfile.mkdtemp(prefix="meta-", dir=ctx.paths.tmp_dir))
    try:
        with ui.progress("Kiadás ellenőrzése", f"{repo} {release.tag}") as bar:
            bar.update(5, "A leíró és az ellenőrző összegek letöltése...")
            meta = installer.fetch_metadata(repo, release, work_dir)
            bar.update(100)
        manifest = meta.manifest
        if manifest.warnings:
            ui.message("\n".join(manifest.warnings), title="A deploy.json figyelmeztetései")

        target = _choose_target(ctx, manifest, repo, preset)
        existing = load_install_info(target)
        port = int(
            ask_validated(
                ui,
                "Melyik porton figyeljen az alkalmazás (csak 127.0.0.1)?",
                _port_error,
                title="Port",
                default=str(
                    (existing.port if existing and existing.port else None) or manifest.port_default
                ),
            )
        )
        plan = build_plan(
            PlanRequest(
                repo=repo,
                version=_version(release),
                target=str(target),
                manifest=manifest,
                target_state=inspect_target(target),
                registry=ctx.registry(),
                requested_migrations=meta.migrations,
                backups=(
                    backup_refs(target / "backups", existing.database.name)
                    if existing and existing.database
                    else ()
                ),
            )
        )
        problems = installer.preflight(meta, release, target, kind=plan.kind, port=port)
        text = format_plan(plan)
        if problems:
            text += "\n\nElőellenőrzés:\n" + "\n".join(f"  ✗ {p}" for p in problems)
        ui.message(text, title="Terv", scroll=True)
        if not plan.can_proceed or problems:
            return

        choice = ui.menu(
            "Mehet?",
            [Item("run", "Folytatás"), Item("plan", "Csak terv (itt megállok)")],
        )
        if choice == "plan":
            return
        installer.confirm_plan(plan)
        result = installer.install(InstallJob(repo, release, meta, plan, target, port))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    ctx.log.info(f"Kész: {manifest.name} {result.version}")
    ui.message(_summary(manifest, result), title="Kész", scroll=True)
    _offer_known_repo(ctx, repo, str(target))


# ------------------------------------------------------------------------- the steps


def _port_error(value: str) -> str | None:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return None
    return "A port 1 és 65535 közötti szám legyen."


def _version(release: Release) -> Version:
    version = release.version
    if version is None:
        raise TelepitoError(f"A(z) {release.tag} címke nem érvényes verzió (SemVer).")
    return version


def _choose_repo(ctx: Context) -> str:
    known = ctx.known_repos()
    installed = {r.repo for r in ctx.registry()}
    items = [Item(k.repo, k.repo + ("  [telepítve]" if k.repo in installed else "")) for k in known]
    items.append(Item(OTHER_REPO, "Másik repó..."))
    choice = ctx.ui.menu("Melyik repó?", items)
    if choice != OTHER_REPO:
        return choice
    return ask_validated(
        ctx.ui,
        "A repó neve (owner/repo):",
        lambda v: None if is_repo(v.strip()) else "A formátum: owner/repo.",
        title="Másik repó",
    ).strip()


def _find_release(github: GitHub, repo: str, tag: str) -> Release:
    for release in github.list_releases(repo):
        if release.tag == tag:
            return release
    raise TelepitoError(f"A(z) {tag} kiadás nem található a(z) {repo} repóban.")


def _choose_release(ctx: Context, github: GitHub, repo: str) -> Release:
    ui = ctx.ui
    releases = [r for r in github.list_releases(repo) if r.version is not None]
    if not releases:
        raise TelepitoError(f"A(z) {repo} repóban nincs telepíthető (SemVer) kiadás.")
    installed = {r.version for r in ctx.registry() if r.repo == repo}
    show_prereleases = False
    default: str | None = None
    while True:
        visible = [r for r in releases if show_prereleases or not r.prerelease]
        items = [
            Item(
                TOGGLE_PRERELEASES,
                f"Előzetes kiadások: {'mutatva' if show_prereleases else 'rejtve'} (váltás)",
            )
        ]
        for release in visible:
            label = f"{release.tag:<14} {format_age(release.published_at, ctx.clock())}"
            if release.prerelease:
                label += "  [előzetes]"
            if str(release.version) in installed:
                label += "  [telepítve]"
            items.append(Item(release.tag, label))
        choice = ui.menu(f"{repo} — kiadások", items, default=default)
        default = choice
        if choice == TOGGLE_PRERELEASES:
            show_prereleases = not show_prereleases
            default = None
            continue
        release = next(r for r in releases if r.tag == choice)
        if _release_action(ctx, release):
            return release


def _release_action(ctx: Context, release: Release) -> bool:
    """Install or read the changelog of one release. ``False`` goes back to the list."""
    while True:
        try:
            action = ctx.ui.menu(
                release.tag,
                [Item("install", "Telepítés ezzel a verzióval"), Item("notes", "Változásjegyzék")],
            )
        except UiBack:
            return False
        if action == "install":
            return True
        ctx.ui.message(
            release.body.strip() or "(Nincs változásjegyzék.)", title=release.tag, scroll=True
        )


def _choose_target(ctx: Context, manifest: Manifest, repo: str, preset: Preset | None) -> Path:
    registry = ctx.registry()
    own = next((r for r in registry if r.name == manifest.name and r.repo == repo), None)
    suggestion = next((k.default_path for k in ctx.known_repos() if k.repo == repo), None)
    default = (
        (preset.target if preset else None)
        or (own.path if own else None)
        or suggestion
        or f"/var/www/{manifest.name}"
    )
    while True:
        text = ctx.ui.text("Hova telepítsem?", title="Célmappa", default=default)
        check = validate_target(
            text,
            state_dir=ctx.paths.state_dir,
            registry=registry,
            own_path=own.path if own else None,
        )
        if check.path is not None:
            return check.path
        ctx.ui.message(check.error or "Érvénytelen mappa.", title="Célmappa")
        default = text


def _summary(manifest: Manifest, result: InstallResult) -> str:
    lines = [f"{manifest.display_name} {result.version} telepítve, és fut: 127.0.0.1:{result.port}"]
    if result.proxy_snippet:
        lines += ["", manifest.proxy_note or "Apache-beállítás (a telepítő nem módosítja):", ""]
        lines += [f"    {line}" for line in result.proxy_snippet.splitlines()]
    if result.post_install:
        lines += ["", "Teendők:"] + [f"  - {step}" for step in result.post_install]
    if result.backup_path is not None:
        lines += ["", f"Adatbázis-mentés: {result.backup_path}"]
    return "\n".join(lines)


def _offer_known_repo(ctx: Context, repo: str, target: str) -> None:
    known = ctx.known_repos()
    if any(k.repo == repo for k in known):
        return
    if ctx.ui.confirm(
        f"Felveszed a(z) {repo} repót az ismert repók közé?", title="Ismert repók", default=True
    ):
        save_repos(ctx.paths.repos_file, [*known, KnownRepo(repo, target)])


__all__ = ["Preset", "UserAbort", "install_flow"]
