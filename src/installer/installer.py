"""Carrying out an installation, an update or a rollback (specification 6.3, 6.4, 6.6).

The decisions are made by :mod:`installer.plan`; this module executes them. All outside
effects go through injected collaborators (GitHub, Docker, the UI, the database and
environment extension points), so the whole flow is testable with fakes.

Order of a run: lock -> download and verify -> compose review -> ``.env`` -> database ->
load image -> activate release -> start -> health check -> switch ``current`` and write the
state. The ``current`` link is switched only after a successful health check, so an
interrupted or failed run never changes what is considered installed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import socket
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backup import backup_refs, offer_retention
from .composediff import review_compose, review_new_compose
from .docker import Docker, DockerError
from .envconfig import EnvConfigurator
from .envfile import read_env, update_env_file
from .errors import InstallerError, UserAbort
from .fsutil import atomic_symlink
from .github import Asset, GitHub, Release
from .health import HealthChecker, http_health, is_healthy
from .lock import FileLock
from .logging_ import Logger
from .manifest import BASE_PATH_ENV, Manifest, ManifestError, load_manifest
from .plan import ConfirmKind, Plan, PlanKind, PlanRequest, build_plan, format_plan
from .state import (
    DatabaseInfo,
    InstallInfo,
    InstallRecord,
    inspect_target,
    load_install_info,
    load_installs,
    save_install_info,
    save_installs,
    upsert_install,
)
from .ui.base import Item, Ui, UiBack
from .versions import Version
from .volumebackup import VolumeBackups
from .volumes import (
    DATA_DIR_ENV,
    GID_ENV,
    UID_ENV,
    ResolvedVolume,
    VolumeCheck,
    check_volumes,
    describe,
    parse_compose_config,
)

GIB = 1024**3
MIB = 1024**2
CHECK_PLACEHOLDER_NETWORK = "installer-check"
CONTRACT_ASSETS = (
    "deploy.json",
    "docker-compose.deploy.yml",
    "image.tar.gz",
    "migrations.txt",
    "SHA256SUMS",
)
IMAGE_ASSET = "image.tar.gz"
SUMS_ASSET = "SHA256SUMS"
MANIFEST_ASSET = "deploy.json"
ENV_LINK_TARGET = "../../shared/.env.production"
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64})[ \t]+\*?(\S.*)$")
_MIGRATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_READ_CHUNK = 1024 * 1024
_FAILURE_LOG_LINES = 80


class ReleaseError(InstallerError):
    """The release does not meet the contract (missing asset, bad checksum, bad compose)."""


class InstallFailed(InstallerError):
    """The run failed after changes were made; the message says what state things are in."""


# --------------------------------------------------------------------- extension points


@dataclass(frozen=True)
class DatabaseSetup:
    info: DatabaseInfo
    env: dict[str, str]  # variables to add to .env.production (the database URL)


class DatabaseOps(ABC):
    """Implemented by the PostgreSQL code (T.4). Passwords never appear in return values."""

    @abstractmethod
    def precheck(self, manifest: Manifest) -> list[str]:
        """Problems that would block a new installation (e.g. no Postgres container)."""

    @abstractmethod
    def create(self, manifest: Manifest) -> DatabaseSetup:
        """Interactively choose a server and create the database and role."""

    @abstractmethod
    def backup(
        self, info: DatabaseInfo, version: str, backups_dir: Path, now: datetime | None = None
    ) -> Path | None:
        """Dump the database; the file name carries ``version`` (the pre-change version).

        ``now`` is the timestamp for the file name, so that a database dump and the volume
        archives of the same run share it (one backup set); default: the current time.

        Returns the file, or ``None`` if the backup failed *and the user explicitly chose to
        continue without one*. A refusal to continue raises :class:`UserAbort`.
        """

    @abstractmethod
    def restore(
        self, info: DatabaseInfo, backup: Path, prepare: Callable[[], None] | None = None
    ) -> None:
        """Restore a dump. Implementations ask for a strong confirmation themselves.

        ``prepare`` (e.g. "stop the application") runs after the confirmation and the safety
        copy, immediately before the first destructive step.
        """

    @abstractmethod
    def rotate_password(self, manifest: Manifest, info: DatabaseInfo, env_path: Path) -> None:
        """Change the database password and the database URL in ``env_path`` together."""


def _default_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _default_disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    github: GitHub
    docker: Docker
    ui: Ui
    log: Logger
    state_dir: Path  # the installer directory: installs.json, .lock
    env: EnvConfigurator
    db: DatabaseOps | None = None
    health: HealthChecker = http_health
    port_in_use: Callable[[int], bool] = _default_port_in_use
    disk_free: Callable[[Path], int] = _default_disk_free
    clock: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


# ----------------------------------------------------------------------- release helpers


@dataclass(frozen=True)
class ReleaseMeta:
    """The verified small files of a release (everything except the image)."""

    manifest: Manifest
    migrations: tuple[str, ...]
    sums: dict[str, str]
    directory: Path  # holds deploy.json, the compose file, migrations.txt, SHA256SUMS


def parse_sums(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` output into ``{file name: hex digest}``."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _SUMS_LINE.match(line.strip())
        if match is None:
            raise ReleaseError("The SHA256SUMS file has an invalid format.")
        name = match.group(2)
        if "/" in name or "\\" in name or ".." in name:
            raise ReleaseError(f"SHA256SUMS contains an invalid file name: {name!r}")
        sums[name] = match.group(1).lower()
    return sums


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected: str, name: str) -> None:
    if not hmac.compare_digest(sha256_file(path), expected.lower()):
        raise ReleaseError(f"The SHA-256 sum of {name} does not match; the download was deleted.")


def parse_migrations(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if not name:
            continue
        if not _MIGRATION_NAME.match(name):
            raise ReleaseError(f"Invalid migration name in migrations.txt: {name!r}")
        names.append(name)
    return tuple(names)


def missing_assets(release: Release, names: tuple[str, ...] = CONTRACT_ASSETS) -> list[str]:
    return [name for name in names if release.asset(name) is None]


@dataclass(frozen=True)
class Layout:
    """The paths inside an installation target (specification 4)."""

    root: Path

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def env_file(self) -> Path:
        return self.shared / ".env.production"

    @property
    def compose_env(self) -> Path:
        return self.shared / "compose.env"

    @property
    def data_dir(self) -> Path:
        """``${DATA_DIR}``: the only place bind-mounted data may live. Never rotated or removed."""
        return self.shared / "data"

    def release_dir(self, version: str) -> Path:
        return self.releases / version

    def staging_dir(self, version: str) -> Path:
        return self.releases / f".tmp-{version}"


@dataclass(frozen=True)
class InstallJob:
    repo: str
    release: Release
    meta: ReleaseMeta
    plan: Plan
    target: Path
    port: int


@dataclass(frozen=True)
class InstallResult:
    version: str
    port: int
    proxy_snippet: str
    post_install: tuple[str, ...]
    backup_path: Path | None = None
    notes: tuple[str, ...] = ()
    volume_backups: tuple[Path, ...] = ()


@dataclass
class _Run:
    """The state of one run, passed between the steps."""

    manifest: Manifest
    version: Version
    repo: str
    kind: PlanKind
    layout: Layout
    port: int
    migrations: tuple[str, ...]
    prior: InstallInfo | None
    db_info: DatabaseInfo | None = None
    backup_path: Path | None = None
    auto_delete_backups: bool = False
    notes: list[str] = field(default_factory=list)
    # The new release's data volumes, resolved from its compose file (checked at review time).
    volumes: tuple[ResolvedVolume, ...] = ()
    # Archives made before the change: the installed release's volume and its archive.
    volume_backups: list[tuple[ResolvedVolume, Path]] = field(default_factory=list)
    # Set while the installed application is stopped for a consistent volume backup.
    stopped: tuple[str, Path] | None = None  # (project, compose file) to start again

    @property
    def image_ref(self) -> str:
        return f"{self.manifest.image_name}:{self.version.image_tag}"


@dataclass(frozen=True)
class InstalledData:
    """The data of an installed application: database and resolved volumes."""

    info: InstallInfo
    manifest: Manifest
    layout: Layout
    volumes: tuple[ResolvedVolume, ...]
    image: str

    @property
    def compose_file(self) -> Path:
        return self.layout.release_dir(self.info.version) / self.manifest.compose_file


@dataclass(frozen=True)
class RestoreChoice:
    """What to restore: a database dump and/or volume archives (volume name -> file)."""

    dump: Path | None = None
    volumes: tuple[tuple[str, Path], ...] = ()


# --------------------------------------------------------------------------- the installer


class Installer:
    def __init__(self, deps: Deps) -> None:
        self.d = deps

    # ------------------------------------------------------------- before the plan

    def fetch_metadata(self, repo: str, release: Release, directory: Path) -> ReleaseMeta:
        """Download and verify the small files of ``release`` into ``directory``.

        Works before any decision is made and writes nothing into an installation target.
        """
        missing = missing_assets(release)
        if missing:
            raise ReleaseError("Assets missing from the release: " + ", ".join(missing))
        directory.mkdir(parents=True, exist_ok=True)

        sums_path = directory / SUMS_ASSET
        self._download(repo, release, SUMS_ASSET, sums_path)
        sums = parse_sums(sums_path.read_text(encoding="utf-8"))

        manifest_path = self._download_verified(repo, release, MANIFEST_ASSET, directory, sums)
        try:
            manifest = load_manifest(manifest_path.read_bytes())
        except ManifestError:
            manifest_path.unlink(missing_ok=True)
            raise
        for warning in manifest.warnings:
            self.d.log.warning(warning)

        self._download_verified(repo, release, manifest.compose_file, directory, sums)
        migrations: tuple[str, ...] = ()
        if manifest.migrations_asset is not None:
            path = self._download_verified(
                repo, release, manifest.migrations_asset, directory, sums
            )
            migrations = parse_migrations(path.read_text(encoding="utf-8"))
        if IMAGE_ASSET not in sums:
            raise ReleaseError("SHA256SUMS does not contain the sum of image.tar.gz.")
        return ReleaseMeta(manifest, migrations, sums, directory)

    def preflight(
        self, meta: ReleaseMeta, release: Release, target: Path, *, kind: PlanKind, port: int
    ) -> list[str]:
        """Read-only checks (6.2) that need the manifest. Returns the problems found."""
        problems: list[str] = []
        try:
            self.d.docker.info()
        except DockerError as exc:
            problems.append(str(exc))
        image = release.asset(IMAGE_ASSET)
        needed = (image.size if image else 0) * 3 + GIB
        roots = [("the target directory", target)]
        try:
            docker_root = self.d.docker.root_dir()
        except DockerError:
            docker_root = None
        if docker_root:
            roots.append(("the Docker root", Path(docker_root)))
        for label, path in roots:
            free = self.d.disk_free(path)
            if free < needed:
                problems.append(
                    f"Not enough free space ({label}): {free // 2**20} MB free, "
                    f"{needed // 2**20} MB needed."
                )
        if kind is not PlanKind.NEW:
            need = self._volume_backup_bytes(target)
            if need:
                free = self.d.disk_free(target)
                if free < need + 64 * MIB:
                    problems.append(
                        f"Not enough free space for the volume backups (the target directory): "
                        f"{free // MIB} MB free, about {need // MIB + 64} MB needed."
                    )
        if kind is PlanKind.NEW and self.d.port_in_use(port):
            problems.append(f"Port {port} is in use. Choose another port.")
        if meta.manifest.database is not None and kind is PlanKind.NEW:
            if self.d.db is None:
                problems.append(
                    "The application needs a database, but no database support is set up."
                )
            else:
                problems += self.d.db.precheck(meta.manifest)
        return problems

    def _volume_backup_bytes(self, target: Path) -> int:
        """The size of the installed volumes that will be archived (0 if unknown or none)."""
        try:
            data = self.installed_data(target)
        except InstallerError:
            return 0
        helper = VolumeBackups(self.d.docker, self.d.ui, self.d.log)
        total = 0
        for volume in data.volumes:
            if volume.spec.backup:
                total += helper.size_bytes(volume, data.image) or 0
        return total

    def confirm_plan(self, plan: Plan) -> None:
        """Ask the confirmation the plan requires; raise :class:`UserAbort` if declined."""
        confirmation = plan.confirmation
        if confirmation is None:
            return
        if confirmation.kind is ConfirmKind.CONFIRM:
            if not self.d.ui.confirm(confirmation.prompt):
                raise UserAbort("The user cancelled the operation.")
            return
        typed = self.d.ui.text(confirmation.prompt)
        if not confirmation.accepts(typed):
            raise UserAbort("The confirmation word does not match; operation cancelled.")

    # ------------------------------------------------------------------ install/update

    def install(self, job: InstallJob) -> InstallResult:
        """Run a confirmed plan (NEW, UPDATE, SAME or DOWNGRADE)."""
        if not job.plan.can_proceed:
            raise InstallerError("The plan has blockers; the operation cannot be carried out.")
        if not job.target.is_dir():
            raise InstallerError(
                f"The target directory ({job.target}) does not exist. Create it with root privileges, e.g.: "
                f"sudo mkdir -p {job.target} && sudo chown $USER {job.target}"
            )
        self.d.state_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.d.state_dir / ".lock"):
            try:
                return self._install_locked(job)
            except KeyboardInterrupt:
                self.d.log.warning(
                    "Interrupted (Ctrl+C). The current link did not change; the installation can be restarted."
                )
                raise

    def _install_locked(self, job: InstallJob) -> InstallResult:
        log = self.d.log
        manifest = job.meta.manifest
        version = Version.parse(job.plan.requested_version)
        layout = Layout(job.target)
        prior = load_install_info(job.target)
        run = _Run(
            manifest=manifest,
            version=version,
            repo=job.repo,
            kind=job.plan.kind,
            layout=layout,
            port=job.port,
            migrations=job.meta.migrations,
            prior=prior,
        )
        log.info(f"{job.plan.kind.value}: {manifest.name} {version} -> {job.target}")
        for directory in (layout.releases, layout.shared, layout.backups):
            directory.mkdir(parents=True, exist_ok=True)

        staging = self._stage(job, layout, version)
        self._review_compose(run, staging)
        try:
            self._configure_env(run)
            self._database_step(run)
            self._load_image(run, staging)
            release_dir = self._activate(run, staging)
        except BaseException:
            self._start_stopped_again(run)
            raise
        return self._start_and_finalize(run, release_dir)

    def _stage(self, job: InstallJob, layout: Layout, version: Version) -> Path:
        """Copy the verified small files and download the verified image into a temp dir."""
        manifest, meta = job.meta.manifest, job.meta
        staging = layout.staging_dir(version.image_tag)
        staging.mkdir(parents=True, exist_ok=True)
        names = [MANIFEST_ASSET, manifest.compose_file, SUMS_ASSET]
        if manifest.migrations_asset is not None:
            names.append(manifest.migrations_asset)
        for name in names:
            shutil.copy2(meta.directory / name, staging / name)
        image_path = staging / IMAGE_ASSET
        expected = meta.sums[IMAGE_ASSET]
        if image_path.exists() and sha256_file(image_path) == expected:
            self.d.log.info("The image is already downloaded and verified (restarted run).")
        else:
            asset = job.release.asset(IMAGE_ASSET)
            assert asset is not None
            self._download(job.repo, job.release, IMAGE_ASSET, image_path, show_progress=True)
            try:
                verify_file(image_path, expected, IMAGE_ASSET)
            except ReleaseError:
                image_path.unlink(missing_ok=True)
                raise
        return staging

    def _review_compose(self, run: _Run, staging: Path) -> None:
        manifest = run.manifest
        new_text = (staging / manifest.compose_file).read_text(encoding="utf-8")
        old_text: str | None = None
        if run.kind is not PlanKind.NEW and run.prior is not None:
            old_path = run.layout.release_dir(run.prior.version) / manifest.compose_file
            try:
                old_text = old_path.read_text(encoding="utf-8")
            except OSError:
                old_text = ""  # unknown previous file: show everything as new
        review = review_compose(old_text, new_text)
        if review.build_present:
            raise ReleaseError(
                "The release's docker-compose file contains `build:`, which the contract forbids "
                "(only prebuilt images may be used)."
            )
        check = self._check_volumes(run, staging)
        run.volumes = check.resolved
        if old_text is None:
            self._review_new_compose(run, new_text, check)
            return
        if review.identical:
            self.d.ui.message("The docker-compose file did not change.", title="Compose review")
            return
        self.d.ui.message(review.diff, title="Changes to the docker-compose file", scroll=True)
        self.d.log.info("Compose diff:\n" + review.diff)
        if review.needs_confirmation and not self.d.ui.confirm(
            "The release may contain a dangerous setting (the lines marked !!). Continue anyway?",
            title="Compose review",
        ):
            raise UserAbort("The user rejected the changes to the compose file.")

    def _check_volumes(self, run: _Run, staging: Path) -> VolumeCheck:
        """Check the volumes of the staged compose file against ``deploy.json`` (blockers raise).

        ``docker compose config`` needs the variables of ``compose.env`` and the ``env_file``,
        none of which exist yet for the new release, so a throw-away set is written into the
        staging directory (and removed again).
        """
        manifest, layout = run.manifest, run.layout
        compose_file = staging / manifest.compose_file
        check_env = staging / ".check.env"
        dummy_env = staging / ".env.production"
        made_dummy = not dummy_env.exists()
        try:
            update_env_file(
                check_env,
                {
                    "APP_VERSION": run.version.image_tag,
                    manifest.port_env: str(run.port),
                    "DB_NETWORK": CHECK_PLACEHOLDER_NETWORK,
                    **self._data_env(layout),
                },
            )
            if made_dummy:
                dummy_env.write_text("", encoding="utf-8")
            try:
                config = self.d.docker.compose_config(
                    manifest.project_name, compose_file, check_env
                )
            except DockerError as exc:
                raise ReleaseError(f"The release's compose file is not usable: {exc}") from exc
        finally:
            check_env.unlink(missing_ok=True)
            if made_dummy:
                dummy_env.unlink(missing_ok=True)
        try:
            model = parse_compose_config(config)
        except ValueError as exc:
            raise ReleaseError(f"The compose configuration cannot be understood: {exc}") from exc
        check = check_volumes(
            model, manifest.volumes, project=manifest.project_name, data_dir=str(layout.data_dir)
        )
        if check.problems:
            raise ReleaseError(
                "The release's volumes do not meet the contract:\n  - "
                + "\n  - ".join(check.problems)
            )
        return check

    def _review_new_compose(self, run: _Run, new_text: str, check: VolumeCheck) -> None:
        """A new installation has no old file to compare with: show what matters, ask once."""
        lines: list[str] = []
        if run.volumes:
            lines.append("This application stores data outside its image:")
            lines += [f"  {describe(v.spec)}" for v in run.volumes]
            lines.append(
                "The data of every volume marked 'backed up' is archived before each update."
            )
        if check.anonymous:
            lines.append("")
            lines.append("Unnamed volumes (NOT backed up, lost when the container is recreated):")
            lines += [f"  {m.service}: {m.target}" for m in check.anonymous]
        review = review_new_compose(new_text)
        if review.flagged:
            lines += ["", "Settings in the compose file that deserve a look (marked !!):"]
            lines += [f"  !! {line}" for line in review.flagged]
        if not lines:
            return
        self.d.ui.message("\n".join(lines), title="Compose review", scroll=True)
        self.d.log.info("New installation compose review:\n" + "\n".join(lines))
        if not self.d.ui.confirm("Continue with these settings?", title="Compose review"):
            raise UserAbort("The user rejected the compose file.")

    def _previous_names(self, run: _Run) -> set[str]:
        """Variable names the currently installed release declared (for removal detection)."""
        if run.prior is None:
            return set()
        path = run.layout.release_dir(run.prior.version) / MANIFEST_ASSET
        try:
            old = load_manifest(path.read_bytes())
        except (OSError, ManifestError):
            return set()
        return {item.name for item in old.env}

    def _configure_env(self, run: _Run) -> None:
        existing = read_env(run.layout.env_file)
        changes = self.d.env.configure(
            run.manifest,
            existing,
            is_new=run.kind is PlanKind.NEW,
            previous_names=self._previous_names(run),
        )
        update_env_file(run.layout.env_file, changes.set, changes.remove)
        self.d.log.info(
            f".env.production: {len(changes.set)} variables set, {len(changes.remove)} removed."
        )

    def _database_step(self, run: _Run) -> None:
        """Create the database (new installation) or back up the data before a change."""
        manifest = run.manifest
        if manifest.database is not None:
            if self.d.db is None:
                raise InstallerError(
                    "The application needs a database, but no database support is set up."
                )
            if run.kind is PlanKind.NEW:
                setup = self.d.db.create(manifest)
                update_env_file(run.layout.env_file, setup.env)
                run.db_info = setup.info
                return
            assert run.prior is not None
            run.db_info = run.prior.database
            if run.prior.database is None:
                raise InstallerError("The installation record is missing the database details.")
        if run.kind is not PlanKind.NEW and run.prior is not None:
            self._backup_data(run, run.prior)

    def _backup_data(self, run: _Run, prior: InstallInfo) -> None:
        """Back up the database and the data volumes of the installed release before changing
        anything (one backup set: shared time stamp and version), then offer to clean up old
        backups (7.3, 7.5)."""
        d = self.d
        now = d.clock()
        database = prior.database if run.manifest.database is not None else None
        if database is not None:
            assert d.db is not None
            run.backup_path = d.db.backup(database, prior.version, run.layout.backups, now)
        volumes = self._backup_volumes(run, prior, now)
        if database is None and not volumes:
            return
        run.auto_delete_backups = offer_retention(
            d.ui,
            d.log,
            run.layout.backups,
            auto_delete=prior.auto_delete_old_backups,
            now=now,
            database=database.name if database is not None else None,
        )

    def installed_data(self, target: Path) -> InstalledData:
        """The installed release's descriptor and its volumes resolved through ``compose config``.

        Raises :class:`InstallerError` if nothing is installed or the descriptor is unreadable.
        A volume that the compose file no longer mounts is simply absent from the result.
        """
        info = load_install_info(target)
        if info is None:
            raise InstallerError("There is no installation in the target directory.")
        layout = Layout(target)
        release_dir = layout.release_dir(info.version)
        try:
            manifest = load_manifest((release_dir / MANIFEST_ASSET).read_bytes())
        except (OSError, ManifestError) as exc:
            raise InstallerError(
                f"The installed release's descriptor is unreadable: {exc}"
            ) from exc
        image = f"{manifest.image_name}:{Version.parse(info.version).image_tag}"
        volumes: tuple[ResolvedVolume, ...] = ()
        if manifest.volumes:
            try:
                config = self.d.docker.compose_config(
                    manifest.project_name, release_dir / manifest.compose_file, layout.compose_env
                )
                model = parse_compose_config(config)
            except (DockerError, ValueError) as exc:
                self.d.log.warning(
                    f"The volumes of the installed release cannot be resolved: {exc}"
                )
            else:
                volumes = check_volumes(
                    model,
                    manifest.volumes,
                    project=manifest.project_name,
                    data_dir=str(layout.data_dir),
                ).resolved
        return InstalledData(info, manifest, layout, volumes, image)

    def _backup_volumes(
        self, run: _Run, prior: InstallInfo, now: datetime
    ) -> list[tuple[ResolvedVolume, Path]]:
        """Archive the installed release's volumes, with the application stopped meanwhile."""
        try:
            data = self.installed_data(run.layout.root)
        except InstallerError as exc:
            self.d.log.warning(f"The volumes cannot be backed up: {exc}")
            return []
        wanted = [v for v in data.volumes if v.spec.backup]
        if not wanted:
            return []
        self._stop_for_backup(run, data)
        run.volume_backups = VolumeBackups(self.d.docker, self.d.ui, self.d.log).backup_all(
            wanted, data.image, prior.version, run.layout.backups, now
        )
        return run.volume_backups

    def _stop_for_backup(self, run: _Run, data: InstalledData) -> None:
        """Stop the installed application so the volume contents do not change while copied."""
        try:
            self.d.docker.compose_stop(
                data.manifest.project_name, data.compose_file, data.layout.compose_env
            )
        except DockerError as exc:
            self.d.log.warning(f"Stopping the application for the backup failed: {exc}")
            return
        run.stopped = (data.manifest.project_name, data.compose_file)
        self.d.log.info("The application was stopped for the volume backup.")

    def _start_stopped_again(self, run: _Run) -> None:
        """After an abort: start the application that was stopped only for the backup."""
        if run.stopped is None:
            return
        project, compose_file = run.stopped
        run.stopped = None
        try:
            self.d.docker.compose_up(project, compose_file, run.layout.compose_env)
        except DockerError as exc:
            self.d.log.error(f"Starting the application again failed: {exc}")
            return
        self.d.log.info("The application was started again.")

    def _load_image(self, run: _Run, staging: Path) -> None:
        docker = self.d.docker
        if docker.image_exists(run.image_ref):
            self.d.log.info(f"The image {run.image_ref} is already loaded.")
        else:
            docker.load_image(staging / IMAGE_ASSET)
            if not docker.image_exists(run.image_ref):
                raise ReleaseError(
                    f"The loaded images do not include {run.image_ref}. "
                    "The release's image tag does not match its version."
                )
        # The image lives in Docker now; no second copy stays on disk.
        (staging / IMAGE_ASSET).unlink(missing_ok=True)

    def _write_compose_env(
        self, layout: Layout, manifest: Manifest, version: Version, port: int,
        db: DatabaseInfo | None,
    ) -> None:  # fmt: skip
        values = {"APP_VERSION": version.image_tag, manifest.port_env: str(port)}
        if db is not None:
            values["DB_NETWORK"] = db.network
        if manifest.volumes:
            values.update(self._data_env(layout))
        update_env_file(layout.compose_env, values)

    @staticmethod
    def _data_env(layout: Layout) -> dict[str, str]:
        """``DATA_DIR`` and the installer user's ids, for the compose file to use."""
        values = {DATA_DIR_ENV: str(layout.data_dir)}
        if hasattr(os, "getuid"):
            values[UID_ENV] = str(os.getuid())
            values[GID_ENV] = str(os.getgid())
        return values

    @staticmethod
    def _prepare_data_dirs(layout: Layout, manifest: Manifest) -> None:
        """Create ``shared/data`` and the directory of every bind volume (owned by the
        installer's user), so Docker does not create them as root."""
        if not any(v.is_bind for v in manifest.volumes):
            return
        layout.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        for volume in manifest.volumes:
            if volume.is_bind and volume.host_path is not None:
                (layout.data_dir / volume.host_path).mkdir(mode=0o750, parents=True, exist_ok=True)

    def _activate(self, run: _Run, staging: Path) -> Path:
        layout = run.layout
        self._prepare_data_dirs(layout, run.manifest)
        release_dir = layout.release_dir(run.version.image_tag)
        if release_dir.exists():
            shutil.rmtree(release_dir)
        staging.rename(release_dir)
        atomic_symlink(release_dir / ".env.production", ENV_LINK_TARGET)
        self._write_compose_env(layout, run.manifest, run.version, run.port, run.db_info)
        return release_dir

    # ----------------------------------------------------- start, health, finalize

    def _start_and_finalize(self, run: _Run, release_dir: Path) -> InstallResult:
        problem = self._up_and_wait(run, release_dir)
        if problem is not None:
            self._handle_failure(run, release_dir, problem)  # always raises
        return self._finalize(run, release_dir)

    def _up_and_wait(self, run: _Run, release_dir: Path) -> str | None:
        """Start the release and wait for health. Returns a problem text, or ``None``."""
        run.stopped = None  # starting the new release replaces the stopped containers
        manifest = run.manifest
        compose_file = release_dir / manifest.compose_file
        try:
            self.d.docker.compose_up(manifest.project_name, compose_file, run.layout.compose_env)
        except DockerError as exc:
            return str(exc)
        return self._wait_healthy(manifest, run.version, run.port, compose_file, run.layout)

    def _wait_healthy(
        self, manifest: Manifest, version: Version, port: int, compose_file: Path, layout: Layout
    ) -> str | None:
        d = self.d
        url = f"http://127.0.0.1:{port}{manifest.health_path}"
        started = d.monotonic()
        deadline = started + manifest.health_timeout_seconds
        last = "not responding yet"
        tick = 0
        with d.ui.progress("Health check", f"{manifest.display_name} is starting...") as bar:
            while True:
                try:
                    response = d.health(url)
                    if is_healthy(response, manifest.health_version_field, version):
                        d.log.info(f"The application is healthy ({url}).")
                        return None
                    last = f"HTTP {response.status}, does not report the expected version"
                except OSError:
                    last = "not responding"
                now = d.monotonic()
                if now >= deadline:
                    return f"The health check timed out ({last})."
                tick += 1
                percent = int((now - started) * 100 / manifest.health_timeout_seconds)
                text = f"{manifest.display_name}: {last}"
                if tick % 5 == 0:
                    text += "\n" + self._logs(manifest, compose_file, layout, tail=3)
                bar.update(min(percent, 99), text)
                d.sleep(1.0)

    def _logs(self, manifest: Manifest, compose_file: Path, layout: Layout, tail: int) -> str:
        try:
            return self.d.docker.compose_logs(
                manifest.project_name, compose_file, layout.compose_env, tail=tail
            )
        except DockerError as exc:
            return f"(the log is not available: {exc})"

    def _handle_failure(self, run: _Run, release_dir: Path, problem: str) -> None:
        """Show the logs, offer recovery, and raise :class:`InstallFailed`.

        Nothing is undone without an explicit choice from the user.
        """
        d, manifest, layout = self.d, run.manifest, run.layout
        compose_file = release_dir / manifest.compose_file
        logs = self._logs(manifest, compose_file, layout, _FAILURE_LOG_LINES)
        d.log.error(f"Start failed: {problem}")
        d.log.output(logs)
        d.ui.message(
            f"{manifest.display_name} {run.version}: {problem}\n\nContainer log:\n{logs}",
            title="Start failed",
            scroll=True,
        )

        items, can_restore_data = self._recovery_items(run)
        try:
            choice = d.ui.menu(
                "What now?",
                items,
                text="The installer never restores anything without your confirmation.",
            )
        except UiBack:
            choice = "keep"

        if choice in ("rollback", "rollback_restore"):
            self._rollback(run, restore_data=choice == "rollback_restore" and can_restore_data)
        if choice == "stop":
            try:
                d.docker.compose_stop(manifest.project_name, compose_file, layout.compose_env)
            except DockerError as exc:
                d.log.error(str(exc))
            raise InstallFailed(
                f"The installation failed and the container was stopped. The release stays at: {release_dir}"
            )
        raise InstallFailed(
            f"The installation failed ({problem}). The failure was kept for inspection: {release_dir}. "
            "The current link did not change."
        )

    def _recovery_items(self, run: _Run) -> tuple[list[Item], bool]:
        prior = run.prior
        items: list[Item] = []
        can_restore_data = False
        if prior is not None and prior.version != str(run.version):
            previous_dir = run.layout.release_dir(prior.version)
            previous_image = f"{run.manifest.image_name}:{Version.parse(prior.version).image_tag}"
            available = previous_dir.is_dir() and self.d.docker.image_exists(previous_image)
            if available:
                # The new release may have migrated the database beyond the old release.
                migrated = run.manifest.database is not None and not set(run.migrations) <= set(
                    prior.migrations
                )
                db_backup = run.backup_path is not None and self.d.db is not None
                volume_backup = bool(run.volume_backups)
                if not migrated:
                    items.append(
                        Item("rollback", f"Roll back to the previous release ({prior.version})")
                    )
                if (migrated and db_backup) or (not migrated and volume_backup):
                    can_restore_data = True
                    items.append(
                        Item(
                            "rollback_restore",
                            f"Previous release ({prior.version}) + restore the data "
                            "(database, volumes) from the backup",
                        )
                    )
        items.append(Item("keep", "Keep the failure for inspection"))
        if run.prior is None:
            items.append(Item("stop", "Stop the container"))
        return items, can_restore_data

    def _rollback(self, run: _Run, *, restore_data: bool) -> None:
        d, layout = self.d, run.layout
        prior = run.prior
        assert prior is not None
        prior_version = Version.parse(prior.version)
        prior_dir = layout.release_dir(prior.version)
        try:
            prior_manifest = load_manifest((prior_dir / MANIFEST_ASSET).read_bytes())
        except (OSError, ManifestError):
            prior_manifest = run.manifest
        failed_dir = layout.release_dir(run.version.image_tag)
        try:
            d.docker.compose_stop(
                run.manifest.project_name,
                failed_dir / run.manifest.compose_file,
                layout.compose_env,
            )
        except DockerError as exc:
            d.log.warning(f"Stopping the new release failed: {exc}")
        if restore_data:
            if run.backup_path is not None and prior.database is not None and d.db is not None:
                d.db.restore(prior.database, run.backup_path)
            if run.volume_backups:
                VolumeBackups(d.docker, d.ui, d.log).restore_all(
                    run.volume_backups,
                    f"{prior_manifest.image_name}:{prior_version.image_tag}",
                    app_name=prior_manifest.name,
                    backups_dir=layout.backups,
                    now=d.clock(),
                )
        self._write_compose_env(
            layout, prior_manifest, prior_version, prior.port or run.port, prior.database
        )
        previous_run = _Run(
            manifest=prior_manifest,
            version=prior_version,
            repo=run.repo,
            kind=PlanKind.DOWNGRADE,
            layout=layout,
            port=prior.port or run.port,
            migrations=prior.migrations,
            prior=None,
        )
        problem = self._up_and_wait(previous_run, prior_dir)
        if problem is not None:
            raise InstallFailed(
                f"Rolling back to the previous release ({prior.version}) failed as well: {problem}"
            )
        d.log.info(f"The previous release ({prior.version}) was restored.")
        raise InstallFailed(
            f"Starting {run.version} failed; the previous release ({prior.version}) "
            "was restored and is running."
        )

    def _finalize(self, run: _Run, release_dir: Path) -> InstallResult:
        d, layout, manifest = self.d, run.layout, run.manifest
        atomic_symlink(layout.current, f"releases/{release_dir.name}")

        previous: list[str] = []
        if run.prior is not None:
            if run.prior.version != str(run.version):
                previous.append(run.prior.version)
            previous += [v for v in run.prior.previous_versions if v != str(run.version)]
        deduped = list(dict.fromkeys(previous))
        info = InstallInfo(
            name=manifest.name,
            repo=run.repo,
            version=str(run.version),
            installed_at=d.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
            previous_versions=tuple(deduped),
            migrations=run.migrations,
            database=run.db_info,
            port=run.port,
            auto_delete_old_backups=run.auto_delete_backups,
        )
        save_install_info(layout.root, info)

        registry_path = d.state_dir / "installs.json"
        records = upsert_install(
            load_installs(registry_path),
            InstallRecord(info.name, run.repo, str(layout.root), info.version, info.installed_at),
        )
        save_installs(registry_path, records)

        self._cleanup(run, info)
        d.log.info(f"Done: {manifest.name} {run.version} is running ({layout.root}).")
        return InstallResult(
            version=str(run.version),
            port=run.port,
            proxy_snippet=manifest.proxy_snippet_for(
                run.port, read_env(layout.env_file).get(BASE_PATH_ENV)
            ),
            post_install=manifest.post_install,
            backup_path=run.backup_path,
            notes=tuple(run.notes),
            volume_backups=tuple(path for _, path in run.volume_backups),
        )

    def _cleanup(self, run: _Run, info: InstallInfo) -> None:
        """Remove old releases and their images beyond ``keep``; the running and previous stay."""
        layout, manifest, log = run.layout, run.manifest, self.d.log
        keep = {info.version, *info.previous_versions[: max(1, manifest.keep)]}
        try:
            entries = [p for p in layout.releases.iterdir() if p.is_dir()]
        except OSError:
            return
        for path in entries:
            name = path.name
            if name.startswith(".tmp-"):
                shutil.rmtree(path, ignore_errors=True)
                continue
            if Version.try_parse(name) is None or name in keep:
                continue
            log.info(f"Deleting old release: {name}")
            shutil.rmtree(path, ignore_errors=True)
            ref = f"{manifest.image_name}:{name}"
            try:
                if self.d.docker.image_exists(ref):
                    self.d.docker.remove_image(ref)
            except DockerError as exc:
                log.warning(f"Deleting the image {ref} failed: {exc}")

    # ---------------------------------------------------------------------- restart

    def restart(self, target: Path) -> str | None:
        """Recreate the containers of the installed release (e.g. after rotating a secret).

        Returns ``None`` if the application is healthy again, else a problem description.
        Nothing is rolled back: the same release is simply started again.
        """
        with FileLock(self._lock_path()):
            return self._restart_locked(target, force_recreate=True)

    def _restart_locked(self, target: Path, *, force_recreate: bool) -> str | None:
        info = load_install_info(target)
        if info is None:
            raise InstallerError("There is no installation in the target directory.")
        layout = Layout(target)
        release_dir = layout.release_dir(info.version)
        try:
            manifest = load_manifest((release_dir / MANIFEST_ASSET).read_bytes())
        except (OSError, ManifestError) as exc:
            raise InstallerError(
                f"The installed release's descriptor is unreadable: {exc}"
            ) from exc
        compose_file = release_dir / manifest.compose_file
        try:
            self.d.docker.compose_up(
                manifest.project_name,
                compose_file,
                layout.compose_env,
                force_recreate=force_recreate,
            )
        except DockerError as exc:
            return str(exc)
        port = info.port or manifest.port_default
        return self._wait_healthy(manifest, Version.parse(info.version), port, compose_file, layout)

    # ------------------------------------------------------------- data backup / restore

    def backup_now(
        self, target: Path, *, stop_app: bool
    ) -> tuple[Path | None, list[Path], str | None]:
        """A manual backup of the database and the volumes (one backup set).

        With ``stop_app`` the application is stopped while the volumes are copied (consistent)
        and started again afterwards. Returns ``(dump, volume archives, start problem)``.
        """
        d = self.d
        with FileLock(self._lock_path()):
            data = self.installed_data(target)
            now = d.clock()
            dump: Path | None = None
            if data.info.database is not None:
                if d.db is None:
                    raise InstallerError("A database backup needs database support.")
                dump = d.db.backup(data.info.database, data.info.version, data.layout.backups, now)
            wanted = [v for v in data.volumes if v.spec.backup]
            stopped = False
            if wanted and stop_app:
                try:
                    d.docker.compose_stop(
                        data.manifest.project_name, data.compose_file, data.layout.compose_env
                    )
                    stopped = True
                except DockerError as exc:
                    d.log.warning(f"Stopping the application for the backup failed: {exc}")
            try:
                made = VolumeBackups(d.docker, d.ui, d.log).backup_all(
                    wanted, data.image, data.info.version, data.layout.backups, now
                )
            finally:
                problem = self._restart_locked(target, force_recreate=False) if stopped else None
        return dump, [path for _, path in made], problem

    def restore_data(self, target: Path, choice: RestoreChoice) -> str | None:
        """Restore a database dump and/or volume archives, then start the application again.

        Every part asks for its own typed confirmation and makes a safety copy first. Returns
        ``None`` if the application is healthy afterwards, else a problem description.
        """
        d = self.d
        with FileLock(self._lock_path()):
            data = self.installed_data(target)

            def stop_application() -> None:
                try:
                    d.docker.compose_stop(
                        data.manifest.project_name, data.compose_file, data.layout.compose_env
                    )
                except DockerError as exc:
                    raise InstallerError(f"Stopping the application failed: {exc}") from exc

            if choice.dump is not None:
                if d.db is None or data.info.database is None:
                    raise InstallerError("This installation has no database to restore.")
                d.db.restore(data.info.database, choice.dump, prepare=stop_application)
            if choice.volumes:
                by_name = {v.name: v for v in data.volumes}
                missing = [name for name, _ in choice.volumes if name not in by_name]
                if missing:
                    raise InstallerError(
                        "The installed release does not use the volume(s): " + ", ".join(missing)
                    )
                VolumeBackups(d.docker, d.ui, d.log).restore_all(
                    [(by_name[name], path) for name, path in choice.volumes],
                    data.image,
                    app_name=data.manifest.name,
                    backups_dir=data.layout.backups,
                    now=d.clock(),
                    prepare=stop_application,
                )
            return self._restart_locked(target, force_recreate=True)

    # -------------------------------------------------------------- rollback (5.4)

    def previous_versions(self, target: Path) -> list[str]:
        """Versions that can be offered for a rollback: release dir *and* image still exist."""
        info = load_install_info(target)
        if info is None:
            return []
        layout = Layout(target)
        result: list[str] = []
        for version in info.previous_versions:
            parsed = Version.try_parse(version)
            if parsed is None or not layout.release_dir(version).is_dir():
                continue
            try:
                manifest = load_manifest(
                    (layout.release_dir(version) / MANIFEST_ASSET).read_bytes()
                )
            except (OSError, ManifestError):
                continue
            if self.d.docker.image_exists(f"{manifest.image_name}:{parsed.image_tag}"):
                result.append(version)
        return result

    def rollback_to(self, target: Path, version_text: str) -> InstallResult:
        """Switch back to an earlier installed release (menu item 4)."""
        with FileLock(self._lock_path()):
            return self._rollback_locked(target, version_text)

    def _lock_path(self) -> Path:
        self.d.state_dir.mkdir(parents=True, exist_ok=True)
        return self.d.state_dir / ".lock"

    def _rollback_locked(self, target: Path, version_text: str) -> InstallResult:
        d = self.d
        info = load_install_info(target)
        if info is None:
            raise InstallerError("There is no installation in the target directory.")
        if version_text not in info.previous_versions:
            raise InstallerError(f"{version_text} is not among the previous versions.")
        layout = Layout(target)
        release_dir = layout.release_dir(version_text)
        version = Version.parse(version_text)
        try:
            manifest = load_manifest((release_dir / MANIFEST_ASSET).read_bytes())
        except (OSError, ManifestError) as exc:
            raise InstallerError(
                f"The directory of release {version_text} is incomplete ({exc}). Download the release again."
            ) from exc
        image_ref = f"{manifest.image_name}:{version.image_tag}"
        if not d.docker.image_exists(image_ref):
            raise InstallerError(
                f"The image {image_ref} is no longer in Docker. Download the release again "
                "through the Install / update menu."
            )
        migrations: tuple[str, ...] | None = None
        if manifest.migrations_asset is not None:
            try:
                migrations = parse_migrations(
                    (release_dir / manifest.migrations_asset).read_text(encoding="utf-8")
                )
            except OSError:
                migrations = None
        else:
            migrations = ()

        plan = build_plan(
            PlanRequest(
                repo=info.repo,
                version=version,
                target=str(target),
                manifest=manifest,
                target_state=inspect_target(target),
                registry=load_installs(d.state_dir / "installs.json"),
                requested_migrations=migrations,
                backups=backup_refs(layout.backups, info.database.name) if info.database else (),
            )
        )
        d.ui.message(format_plan(plan), title="Roll back", scroll=True)
        if not plan.can_proceed:
            raise InstallerError("The rollback cannot be carried out: " + "; ".join(plan.blockers))
        self.confirm_plan(plan)

        port = info.port or manifest.port_default
        run = _Run(
            manifest=manifest,
            version=version,
            repo=info.repo,
            kind=PlanKind.DOWNGRADE,
            layout=layout,
            port=port,
            migrations=migrations or (),
            prior=info,
            db_info=info.database,
            auto_delete_backups=info.auto_delete_old_backups,
        )
        if manifest.database is not None and (d.db is None or info.database is None):
            raise InstallerError("A database backup needs database support, which is not set up.")
        try:
            self._backup_data(run, info)
            self._write_compose_env(layout, manifest, version, port, info.database)
        except BaseException:
            self._start_stopped_again(run)
            raise
        return self._start_and_finalize(run, release_dir)

    # ------------------------------------------------------------------ downloading

    def _download(
        self, repo: str, release: Release, name: str, dest: Path, *, show_progress: bool = False
    ) -> None:
        asset: Asset | None = release.asset(name)
        if asset is None:
            raise ReleaseError(f"The release has no {name} asset.")
        self.d.log.info(f"Downloading {name} ({asset.size} bytes)")
        if not show_progress:
            self.d.github.download_asset(repo, asset, dest)
            return
        with self.d.ui.progress("Download", name) as bar:
            self.d.github.download_asset(
                repo,
                asset,
                dest,
                lambda done, total: bar.update(int(done * 100 / total) if total else 0),
            )

    def _download_verified(
        self, repo: str, release: Release, name: str, directory: Path, sums: dict[str, str]
    ) -> Path:
        expected = sums.get(name)
        if expected is None:
            raise ReleaseError(f"SHA256SUMS does not contain the sum of {name}.")
        path = directory / name
        self._download(repo, release, name, path)
        try:
            verify_file(path, expected, name)
        except ReleaseError:
            path.unlink(missing_ok=True)
            raise
        return path
