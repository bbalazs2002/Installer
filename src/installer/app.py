"""The application context and the main menu (specification 5).

:class:`Context` bundles everything the menu flows need (UI, Docker, token, vault session,
logger) and builds the GitHub client and the installer on demand, so that a token entered in
the settings is picked up immediately.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .docker import Docker
from .envconfig import EnvConfigurator
from .errors import PrerequisiteError, TelepitoError, UserAbort
from .github import GitHub, GitHubAuthError
from .github_http import HttpGitHub
from .health import HealthChecker, http_health
from .installer import MANIFEST_ASSET, DatabaseOps, Deps, Installer, Layout
from .logging_ import Logger
from .manifest import Manifest, load_manifest
from .paths import Paths
from .postgres import PostgresOps
from .selfupdate import running_pyz
from .state import (
    InstallInfo,
    InstallRecord,
    KnownRepo,
    load_install_info,
    load_installs,
    load_repos,
)
from .tokenstore import TokenStore
from .ui.base import Item, Ui, UiBack
from .vault import Vault
from .wizard import InteractiveEnvConfigurator, open_vault_interactive

GithubFactory = Callable[[str], GitHub]


class ExitRequested(Exception):  # noqa: N818 - control flow, not an error
    """Ask the main menu to end (e.g. after a self-update: the new version must be started)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Context:
    paths: Paths
    ui: Ui
    log: Logger
    docker: Docker
    tokens: TokenStore
    github_factory: GithubFactory = HttpGitHub
    health: HealthChecker = http_health
    clock: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    # Optional overrides (tests); ``None`` keeps the Installer's real defaults.
    port_in_use: Callable[[int], bool] | None = None
    disk_free: Callable[[Path], int] | None = None
    env_configurator: EnvConfigurator | None = None
    db_ops: DatabaseOps | None = None
    # The ``telepito.pyz`` being executed (``None`` outside a zipapp); the self-update target.
    pyz_path: Path | None = field(default_factory=running_pyz)
    _vault: Vault | None = field(default=None, repr=False)
    _github: tuple[str, GitHub] | None = field(default=None, repr=False)

    # --------------------------------------------------------------- collaborators

    def github(self) -> GitHub:
        """The GitHub client for the stored token (rebuilt if the token changed)."""
        token = self.tokens.load()
        if token is None:
            raise GitHubAuthError("No GitHub token is set. Enter it in the Settings menu.")
        self.log.redactor.register(token)
        if self._github is None or self._github[0] != token:
            self._github = (token, self.github_factory(token))
        return self._github[1]

    def vault(self) -> Vault | None:
        """The unlocked vault; asks for the master password once per session."""
        if self._vault is None:
            self._vault = open_vault_interactive(
                self.ui, self.paths.vault_file, self.log.redactor, sleep=self.sleep
            )
        return self._vault

    def database_ops(self) -> DatabaseOps:
        if self.db_ops is not None:
            return self.db_ops
        return PostgresOps(
            self.docker, self.ui, self.log, vault_provider=self.vault, clock=self.clock
        )

    def installer(self) -> Installer:
        configurator = self.env_configurator or InteractiveEnvConfigurator(
            self.ui, self.log.redactor, self.vault
        )
        overrides: dict[str, object] = {}
        if self.port_in_use is not None:
            overrides["port_in_use"] = self.port_in_use
        if self.disk_free is not None:
            overrides["disk_free"] = self.disk_free
        deps = Deps(
            github=self.github(),
            docker=self.docker,
            ui=self.ui,
            log=self.log,
            state_dir=self.paths.state_dir,
            env=configurator,
            db=self.database_ops(),
            health=self.health,
            clock=self.clock,
            sleep=self.sleep,
            monotonic=self.monotonic,
            **overrides,  # type: ignore[arg-type]
        )
        return Installer(deps)

    # ------------------------------------------------------------------------ state

    def registry(self) -> list[InstallRecord]:
        return load_installs(self.paths.installs_file)

    def known_repos(self) -> list[KnownRepo]:
        return load_repos(self.paths.repos_file)

    def load_install(self, record: InstallRecord) -> tuple[InstallInfo, Manifest, Layout]:
        """The recorded state and the manifest of the *installed* release."""
        target = Path(record.path)
        info = load_install_info(target)
        if info is None:
            raise TelepitoError(f"There is no installation in {record.path}.")
        layout = Layout(target)
        try:
            manifest = load_manifest(
                (layout.release_dir(info.version) / MANIFEST_ASSET).read_bytes()
            )
        except OSError as exc:
            raise TelepitoError(f"The installed release's descriptor is unreadable: {exc}") from exc
        return info, manifest, layout

    def pick_install(self, title: str, *, with_database: bool = False) -> InstallRecord | None:
        """Choose one installed application (``None`` after telling the user there is none)."""
        records = self.registry()
        if with_database:
            records = [r for r in records if self._has_database(r)]
        if not records:
            self.ui.message("There is no suitable installed application.", title=title)
            return None
        if len(records) == 1:
            return records[0]
        by_name = {r.name: r for r in records}
        key = self.ui.menu(
            title,
            [Item(r.name, f"{r.name}  {r.version}", r.path) for r in records],
            text="Which application?",
        )
        return by_name[key]

    def _has_database(self, record: InstallRecord) -> bool:
        try:
            info = load_install_info(Path(record.path))
        except TelepitoError:
            return False
        return info is not None and info.database is not None


# ------------------------------------------------------------------------- main menu

_MAIN_ITEMS = [
    ("install", "Install / update"),
    ("updates", "Available updates"),
    ("secrets", "Passwords and .env"),
    ("rollback", "Roll back to a previous version"),
    ("database", "Database (backup, restore, delete backups)"),
    ("status", "Status"),
    ("logs", "Logs"),
    ("settings", "Settings (GitHub token, known repositories)"),
    ("selfupdate", "Update the installer"),
    ("quit", "Quit"),
]


def run_menu(ctx: Context, *, updates_hint: Callable[[], int] | None = None) -> int:
    """The interactive main menu. Returns when the user quits (or presses Esc)."""
    from .flows_db import database_menu
    from .flows_env import secrets_menu
    from .flows_install import install_flow
    from .flows_misc import (
        logs_screen,
        rollback_screen,
        selfupdate_screen,
        settings_menu,
        status_screen,
        updates_screen,
    )

    actions: dict[str, Callable[[Context], None]] = {
        "install": lambda c: install_flow(c),
        "updates": updates_screen,
        "secrets": secrets_menu,
        "rollback": rollback_screen,
        "database": database_menu,
        "status": status_screen,
        "logs": logs_screen,
        "settings": settings_menu,
        "selfupdate": selfupdate_screen,
    }
    pending = updates_hint() if updates_hint is not None else 0
    default = "install"
    while True:
        items = []
        for key, label in _MAIN_ITEMS:
            if key == "updates" and pending:
                label += f"  ({pending} updates available)"
            items.append(Item(key, label))
        try:
            choice = ctx.ui.menu(
                "Installer — main menu", items, text=f"telepito {__version__}", default=default
            )
        except UiBack:
            return 0
        if choice == "quit":
            return 0
        default = choice
        try:
            run_action(ctx, actions[choice])
        except ExitRequested:
            return 0
        if choice in ("install", "updates"):
            pending = updates_hint() if updates_hint is not None else 0


def run_action(ctx: Context, action: Callable[[Context], None]) -> None:
    """Run one menu action; Esc goes back, expected errors become a message."""
    try:
        action(ctx)
    except UiBack:
        return
    except UserAbort as exc:
        ctx.log.info(f"Cancelled: {exc}")
        ctx.ui.message(str(exc), title="Cancelled")
    except PrerequisiteError as exc:
        ctx.log.error(str(exc))
        ctx.ui.message(str(exc), title="Missing prerequisite")
    except TelepitoError as exc:
        ctx.log.error(str(exc))
        ctx.ui.message(str(exc), title="Error")
