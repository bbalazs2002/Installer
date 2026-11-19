"""Command-line entry point: the interactive menu plus ``status``, ``plan`` and ``updates``.

Exit codes: 0 success, 1 failure, 2 cancelled by the user, 3 a prerequisite is missing.
Nothing but the menu ever changes anything on the server.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .app import Context, run_menu
from .backup import backup_refs
from .docker import Docker
from .docker_cli import SubprocessDocker
from .errors import (
    EXIT_ERROR,
    EXIT_MISSING_PREREQUISITE,
    EXIT_OK,
    EXIT_USER_ABORT,
    PrerequisiteError,
    TelepitoError,
    UserAbort,
)
from .flows_misc import pending_updates
from .github import GitHub
from .github_http import HttpGitHub
from .health import HealthChecker, http_health
from .logging_ import Logger, Redactor
from .paths import Paths, default_paths
from .plan import PlanRequest, build_plan, format_plan
from .state import inspect_target, load_install_info
from .status import collect_status
from .tokenstore import TokenStore
from .ui.base import Ui, UiBack
from .ui.batch import BatchUi
from .ui.whiptail import WhiptailUi
from .updates import find_updates, format_update_line
from .versions import Version, VersionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telepito",
        description="Telepítő GitHub-release-ekhez (Docker). Paraméter nélkül a menü indul.",
    )
    parser.add_argument("--version", action="version", version=f"telepito {__version__}")
    parser.add_argument("--state-dir", help="a telepítő mappája (alap: /var/www/telepito)")
    parser.add_argument("--token-file", help="a GitHub-token fájlja")
    sub = parser.add_subparsers(dest="command", metavar="parancs")

    status = sub.add_parser("status", help="a telepített alkalmazások állapota (csak olvas)")
    status.add_argument("--json", action="store_true", help="géppel olvasható kimenet")

    plan = sub.add_parser("plan", help="a terv kiírása; semmit nem módosít")
    plan.add_argument("repo", help="owner/repo")
    plan.add_argument("version", help="a kiadás verziója, pl. 0.2.0")
    plan.add_argument("target", help="a célmappa")

    sub.add_parser("updates", help="az elérhető frissítések listája")
    return parser


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_ERROR


def main(
    argv: Sequence[str] | None = None,
    *,
    ui: Ui | None = None,
    docker: Docker | None = None,
    github_factory: Callable[[str], GitHub] = HttpGitHub,
    health: HealthChecker = http_health,
    paths: Paths | None = None,
) -> int:
    """Run the installer. The keyword arguments exist for tests; production passes none."""
    args = build_parser().parse_args(argv)
    for stream in (
        sys.stdout,
        sys.stderr,
    ):  # never crash on a terminal that cannot show a character
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print(
            "Ne futtasd a telepítőt root-ként. Használd a saját felhasználódat; ha root kell "
            "valamihez, a telepítő kiírja a pontos parancsot.",
            file=sys.stderr,
        )
        return EXIT_MISSING_PREREQUISITE

    paths = paths or default_paths(args.state_dir, args.token_file)
    redactor = Redactor()
    log = Logger(paths.logs_dir, redactor)
    try:
        interactive = args.command is None
        if interactive and ui is None:
            ui = _terminal_ui()
        ctx = Context(
            paths=paths,
            ui=ui or BatchUi(),
            log=log,
            docker=docker or SubprocessDocker(redactor=redactor),
            tokens=TokenStore(paths.token_file),
            github_factory=github_factory,
            health=health,
        )
        if interactive:
            _require_state_dir(paths)
            return run_menu(ctx, updates_hint=lambda: pending_updates(ctx))
        if args.command == "status":
            return _status(ctx, as_json=args.json)
        if args.command == "updates":
            return _updates(ctx)
        if args.command == "plan":
            return _plan(ctx, args.repo, args.version, args.target)
    except UiBack:
        return EXIT_OK
    except UserAbort as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USER_ABORT
    except PrerequisiteError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MISSING_PREREQUISITE
    except TelepitoError as exc:
        log.error(str(exc))
        return _fail(str(exc))
    except KeyboardInterrupt:
        print("\nMegszakítva.", file=sys.stderr)
        return EXIT_USER_ABORT
    return EXIT_ERROR


def _terminal_ui() -> Ui:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PrerequisiteError("A menühöz interaktív terminál kell.")
    if not WhiptailUi.available():
        raise PrerequisiteError("A whiptail nem található. Telepítés: sudo apt install whiptail")
    return WhiptailUi()


def _require_state_dir(paths: Paths) -> None:
    if not paths.state_dir.is_dir():
        raise PrerequisiteError(
            f"A telepítő mappája ({paths.state_dir}) nem létezik. Hozd létre root jogosultsággal:\n"
            f"  sudo mkdir -p {paths.state_dir} && sudo chown $USER {paths.state_dir} "
            f"&& sudo chmod 2775 {paths.state_dir}"
        )


def _status(ctx: Context, *, as_json: bool) -> int:
    statuses = collect_status(ctx.registry(), ctx.docker, ctx.health)
    if as_json:
        print(
            json.dumps({"installs": [s.to_json() for s in statuses]}, indent=2, ensure_ascii=False)
        )
        return EXIT_OK
    from .status import format_status

    print(format_status(statuses))
    return EXIT_OK


def _updates(ctx: Context) -> int:
    records = ctx.registry()
    if not records:
        print("Még nincs telepített alkalmazás.")
        return EXIT_OK
    now = ctx.clock()
    for info in find_updates(ctx.github(), records):
        print(format_update_line(info, now))
    return EXIT_OK


def _plan(ctx: Context, repo: str, version_text: str, target_text: str) -> int:
    try:
        version = Version.parse(version_text)
    except VersionError as exc:
        return _fail(str(exc))
    github = ctx.github()
    release = next((r for r in github.list_releases(repo) if r.version == version), None)
    if release is None:
        return _fail(f"A(z) {version} kiadás nem található a(z) {repo} repóban.")

    target = Path(os.path.realpath(target_text))
    ctx.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="plan-", dir=ctx.paths.tmp_dir))
    try:
        installer = ctx.installer()
        meta = installer.fetch_metadata(repo, release, work_dir)
        existing = load_install_info(target) if target.is_dir() else None
        plan = build_plan(
            PlanRequest(
                repo=repo,
                version=version,
                target=str(target),
                manifest=meta.manifest,
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
        port = (existing.port if existing and existing.port else None) or meta.manifest.port_default
        problems = installer.preflight(meta, release, target, kind=plan.kind, port=port)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(format_plan(plan))
    if problems:
        print("\nElőellenőrzés:")
        for problem in problems:
            print(f"  ✗ {problem}")
    return EXIT_OK if plan.can_proceed and not problems else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
