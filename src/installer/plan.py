"""The decision logic: current state + request -> a :class:`Plan` (pure, no I/O).

The planner never touches the disk, the network or Docker. Everything it needs is passed in
(see :class:`PlanRequest`), which makes the whole state matrix of the specification (6.1) and
the rollback rules (6.4) testable without fakes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .manifest import Manifest
from .state import InstallInfo, InstallRecord, TargetState
from .versions import Version

DOWNGRADE_PHRASE = "DOWNGRADE"
_MAX_LISTED_MIGRATIONS = 5


class PlanKind(str, Enum):
    NEW = "NEW"
    UPDATE = "UPDATE"
    SAME = "SAME"
    DOWNGRADE = "DOWNGRADE"
    OTHER_REPO = "OTHER_REPO"
    # The target cannot be understood (e.g. an unreadable ``.install.json``): never guess.
    BLOCKED = "BLOCKED"


class StepId(str, Enum):
    LOCK = "lock"
    DOWNLOAD = "download"
    COMPOSE_REVIEW = "compose_review"
    ENV = "env"
    DB_CREATE = "db_create"
    DB_BACKUP = "db_backup"
    LOAD_IMAGE = "load_image"
    ACTIVATE = "activate"
    START = "start"
    HEALTH = "health"
    FINALIZE = "finalize"


class ConfirmKind(str, Enum):
    CONFIRM = "confirm"  # a plain yes/no
    TYPE = "type"  # the user must type ``phrase``


@dataclass(frozen=True)
class Step:
    id: StepId
    description: str


@dataclass(frozen=True)
class Confirmation:
    kind: ConfirmKind
    prompt: str
    phrase: str = ""

    def accepts(self, typed: str) -> bool:
        """For ``TYPE``: does the typed text match exactly?"""
        return self.kind is ConfirmKind.TYPE and typed.strip() == self.phrase


@dataclass(frozen=True)
class BackupRef:
    """An existing database backup. ``version`` is the version whose data it holds."""

    version: str
    path: str
    created_at: str  # sortable (ISO 8601 UTC)


@dataclass(frozen=True)
class PlanRequest:
    repo: str
    version: Version
    target: str  # normalised (realpath) by the caller
    manifest: Manifest
    target_state: TargetState
    registry: Sequence[InstallRecord] = ()
    # The release's migrations.txt for the *requested* version; None if not available.
    requested_migrations: tuple[str, ...] | None = None
    backups: Sequence[BackupRef] = ()


@dataclass(frozen=True)
class Plan:
    kind: PlanKind
    requested_version: str
    installed_version: str | None
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    steps: tuple[Step, ...]
    confirmation: Confirmation | None
    # Migrations present in the installed version but absent from the requested one.
    extra_migrations: tuple[str, ...] = ()
    # A backup that could replace the (too new) database for a blocked downgrade.
    restore_backup: BackupRef | None = None

    @property
    def can_proceed(self) -> bool:
        return not self.blockers


def build_plan(request: PlanRequest) -> Plan:
    """Decide what kind of operation this is, what to warn about, and what to do."""
    manifest = request.manifest
    state = request.target_state
    requested = request.version
    warnings: list[str] = list(manifest.warnings)
    blockers: list[str] = []

    if requested.is_prerelease:
        warnings.append(f"{requested} is a pre-release.")

    # 1. A target we cannot understand is never guessed around.
    if state.unreadable_reason is not None:
        blockers.append(
            "The installation record (.install.json) in the target directory cannot be understood, "
            f"so I am not continuing: {state.unreadable_reason}"
        )
        return _plan(PlanKind.BLOCKED, request, None, warnings, blockers, None)

    info = state.install_info
    # 2. The compose project name must be unique on the server.
    for record in request.registry:
        same_place = record.path == request.target
        if not same_place and record.name in (manifest.name, manifest.project_name):
            blockers.append(
                f"An installation named {manifest.project_name} already exists elsewhere: {record.path}. "
                "Only one installation per name is possible."
            )

    # 3. Nothing installed in the target yet.
    if info is None:
        if state.exists and not state.empty:
            blockers.append(
                f"The target directory ({request.target}) is not empty and was not created by the installer."
            )
        return _plan(PlanKind.NEW, request, None, warnings, blockers, None)

    # 4. Something is installed: it must be this very application.
    if info.repo != request.repo or info.name != manifest.name:
        blockers.append(
            f"A different application is installed in the target directory ({info.name}, {info.repo}). "
            "Choose another directory."
        )
        return _plan(PlanKind.OTHER_REPO, request, info.version, warnings, blockers, None)

    installed = Version.try_parse(info.version)
    if installed is None:
        blockers.append(f"The installed version ({info.version!r}) cannot be understood.")
        return _plan(PlanKind.BLOCKED, request, info.version, warnings, blockers, None)

    if requested > installed:
        warnings.append(f"This is an update, not a new installation: {installed} → {requested}")
        return _plan(PlanKind.UPDATE, request, str(installed), warnings, blockers, info)
    if requested == installed:
        warnings.append(f"This version is already installed ({installed}).")
        return _plan(PlanKind.SAME, request, str(installed), warnings, blockers, info)
    return _downgrade_plan(request, installed, info, warnings, blockers)


def _downgrade_plan(
    request: PlanRequest,
    installed: Version,
    info: InstallInfo,
    warnings: list[str],
    blockers: list[str],
) -> Plan:
    manifest = request.manifest
    warnings.append(
        f"DOWNGRADE: {installed} → {request.version}. The older release is not necessarily "
        "compatible with the current data, and database migrations only go forward."
    )
    extra: tuple[str, ...] = ()
    restore: BackupRef | None = None

    if manifest.database is not None:
        if request.requested_migrations is None:
            blockers.append(
                "The migration list of the requested version is not available, so it cannot be "
                "determined whether the downgrade is safe."
            )
        else:
            wanted = set(request.requested_migrations)
            extra = tuple(m for m in info.migrations if m not in wanted)
            if extra:
                listed = ", ".join(extra[:_MAX_LISTED_MIGRATIONS])
                more = len(extra) - _MAX_LISTED_MIGRATIONS
                if more > 0:
                    listed += f" (+{more} more)"
                blockers.append(
                    f"The database is newer than the requested version: {len(extra)} migrations are "
                    f"not in the requested version ({listed})."
                )
                restore = _matching_backup(request.backups, request.version)
                if restore is not None:
                    warnings.append(
                        f"Possible: restore the database from the backup of version {restore.version}. "
                        "Data created since that backup "
                        "will be lost."
                    )
                else:
                    warnings.append(
                        f"There is no database backup for version {request.version}, "
                        "so the downgrade is not possible."
                    )
    return _plan(
        PlanKind.DOWNGRADE, request, str(installed), warnings, blockers, info, extra, restore
    )


def _matching_backup(backups: Sequence[BackupRef], version: Version) -> BackupRef | None:
    candidates = [b for b in backups if Version.try_parse(b.version) == version]
    return max(candidates, key=lambda b: b.created_at) if candidates else None


def _plan(
    kind: PlanKind,
    request: PlanRequest,
    installed_version: str | None,
    warnings: list[str],
    blockers: list[str],
    info: InstallInfo | None,
    extra: tuple[str, ...] = (),
    restore: BackupRef | None = None,
) -> Plan:
    steps: tuple[Step, ...] = ()
    confirmation: Confirmation | None = None
    if kind in (PlanKind.NEW, PlanKind.UPDATE, PlanKind.SAME, PlanKind.DOWNGRADE):
        steps = _steps(kind, request.manifest)
        confirmation = _confirmation(kind, request)
    return Plan(
        kind=kind,
        requested_version=str(request.version),
        installed_version=installed_version,
        warnings=tuple(warnings),
        blockers=tuple(blockers),
        steps=steps,
        confirmation=confirmation,
        extra_migrations=extra,
        restore_backup=restore,
    )


def _steps(kind: PlanKind, manifest: Manifest) -> tuple[Step, ...]:
    is_new = kind is PlanKind.NEW
    steps = [
        Step(StepId.LOCK, "Lock (no other installer may run at the same time)"),
        Step(StepId.DOWNLOAD, "Download the release and verify the SHA-256 sums"),
    ]
    if not is_new:
        # Also for SAME: a re-published release could differ from what is installed.
        steps.append(Step(StepId.COMPOSE_REVIEW, "Review the changes to the docker-compose file"))
    steps.append(
        Step(
            StepId.ENV,
            "Set up .env.production (wizard)"
            if is_new
            else "Set up the new environment variables and list the removed ones",
        )
    )
    if manifest.database is not None:
        if is_new:
            steps.append(Step(StepId.DB_CREATE, "Create the database and user"))
        else:
            steps.append(Step(StepId.DB_BACKUP, "Database backup before the change"))
    steps += [
        Step(StepId.LOAD_IMAGE, "Load the Docker image"),
        Step(StepId.ACTIVATE, "Prepare the release (directory, .env.production link, compose.env)"),
        Step(StepId.START, "Start (docker compose up -d)"),
        Step(
            StepId.HEALTH,
            f"Health check ({manifest.health_path}, at most {manifest.health_timeout_seconds} s)",
        ),
        Step(
            StepId.FINALIZE,
            "Switch to the new release, update the records, clean up old releases",
        ),
    ]
    return tuple(steps)


def _confirmation(kind: PlanKind, request: PlanRequest) -> Confirmation:
    name = request.manifest.display_name
    if kind is PlanKind.DOWNGRADE:
        return Confirmation(
            ConfirmKind.TYPE,
            f"Downgrade {name} to version {request.version}. To continue, type: {DOWNGRADE_PHRASE}",
            DOWNGRADE_PHRASE,
        )
    if kind is PlanKind.SAME:
        return Confirmation(
            ConfirmKind.CONFIRM,
            f"Version {request.version} is already installed. Reinstall it?",
        )
    if kind is PlanKind.UPDATE:
        return Confirmation(ConfirmKind.CONFIRM, f"Update {name}?")
    return Confirmation(ConfirmKind.CONFIRM, f"Install version {request.version} of {name}?")


_KIND_TITLES = {
    PlanKind.NEW: "New installation",
    PlanKind.UPDATE: "Update",
    PlanKind.SAME: "Reinstall (same version)",
    PlanKind.DOWNGRADE: "DOWNGRADE",
    PlanKind.OTHER_REPO: "A different application is in the target directory",
    PlanKind.BLOCKED: "The operation cannot be carried out",
}


def format_plan(plan: Plan) -> str:
    """The plan as plain text (for the confirmation screen and ``installer plan``)."""
    lines = [f"Operation: {_KIND_TITLES[plan.kind]}"]
    if plan.installed_version:
        lines.append(f"Installed version: {plan.installed_version}")
    lines.append(f"Requested version: {plan.requested_version}")
    if plan.blockers:
        lines += ["", "Blockers:"] + [f"  ✗ {b}" for b in plan.blockers]
    if plan.warnings:
        lines += ["", "Warnings:"] + [f"  ! {w}" for w in plan.warnings]
    if plan.steps and not plan.blockers:
        lines += ["", "Steps:"] + [f"  {i}. {s.description}" for i, s in enumerate(plan.steps, 1)]
    return "\n".join(lines)
