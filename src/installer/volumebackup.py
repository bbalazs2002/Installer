"""Backing up and restoring the data volumes (compressed ``.tar.gz`` archives).

The archive is made by a throw-away container of the application's own image (so nothing has
to be downloaded): ``tar czf - -C /data .`` writes to the container's standard output, which
the installer streams into a file it created itself with mode 600. Nothing is ever written
into ``backups/`` by root, so the old backups can always be deleted by the installer's user.
Every archive is read back completely before it counts as a backup.

Restoring empties the *contents* of the volume (never the volume itself, nothing is ever
removed with ``docker volume rm``) and unpacks the archive; a safety copy of the current
contents is made first.
"""

from __future__ import annotations

import tarfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from .backup import PRE_RESTORE, create_private_file, volume_backup_filename
from .docker import Docker, DockerError, HelperMount
from .errors import InstallerError, UserAbort
from .logging_ import Logger
from .ui.base import Ui
from .volumes import ResolvedVolume

DATA_MOUNT = "/data"
BACKUP_COMMAND = ("sh", "-c", f"tar czf - -C {DATA_MOUNT} .")
SIZE_COMMAND = ("sh", "-c", f"du -sk {DATA_MOUNT}")
# The glob patterns cover hidden files without matching "." and ".." (POSIX sh, busybox-safe).
RESTORE_COMMAND = (
    "sh",
    "-c",
    f"rm -rf -- {DATA_MOUNT}/* {DATA_MOUNT}/.[!.]* {DATA_MOUNT}/..?* && tar xzf - -C {DATA_MOUNT}",
)


class VolumeBackupError(InstallerError):
    """A volume could not be archived or restored. The message is user-facing."""


def _tail(data: bytes) -> str:
    lines = data.decode("utf-8", errors="replace").strip().splitlines()
    return " ".join(lines[-3:])


def _read_back(path: Path) -> None:
    """Read the whole archive: a truncated or corrupt file fails here."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            for _ in archive:
                pass
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise VolumeBackupError(f"The archive cannot be read back ({exc}).") from exc


class VolumeBackups:
    """Volume backup and restore with the same safeguards as the database backup."""

    def __init__(self, docker: Docker, ui: Ui, log: Logger) -> None:
        self._docker = docker
        self._ui = ui
        self._log = log

    # ------------------------------------------------------------------------ backup

    def archive(
        self, volume: ResolvedVolume, image: str, version: str, directory: Path, now: datetime
    ) -> Path:
        """Archive one volume into ``directory``; raises on any failure (file removed)."""
        if not self._docker.image_exists(image):
            raise VolumeBackupError(f"The image {image} is not in Docker.")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / volume_backup_filename(volume.name, version, now)
        try:
            create_private_file(path)
        except FileExistsError as exc:
            raise VolumeBackupError(
                f"A backup named {path.name} already exists (two backups in the same second); "
                "try again in a moment."
            ) from exc
        try:
            result = self._docker.run_helper(
                image,
                BACKUP_COMMAND,
                mounts=[HelperMount(volume.source, DATA_MOUNT, read_only=True)],
                stdout_to=path,
            )
            if result.returncode != 0:
                raise VolumeBackupError(_tail(result.stderr) or "tar exited with an error.")
            _read_back(path)
        except (VolumeBackupError, DockerError):
            path.unlink(missing_ok=True)
            raise
        self._log.info(f"Volume backup done: {path.name} ({path.stat().st_size} bytes)")
        return path

    def backup_all(
        self,
        volumes: Sequence[ResolvedVolume],
        image: str,
        version: str,
        directory: Path,
        now: datetime,
    ) -> list[tuple[ResolvedVolume, Path]]:
        """Back up every volume that wants it. A failure is explained and the user decides
        whether to go on without that archive; declining raises :class:`UserAbort`."""
        created: list[tuple[ResolvedVolume, Path]] = []
        for volume in volumes:
            if not volume.spec.backup:
                self._log.info(f"Volume {volume.name}: backup is switched off in deploy.json.")
                continue
            try:
                created.append((volume, self.archive(volume, image, version, directory, now)))
            except (VolumeBackupError, DockerError) as exc:
                self._log.error(f"Volume backup failed ({volume.name}): {exc}")
                self._ui.message(
                    f"The backup of the volume {volume.name} failed: {exc}", title="Backup"
                )
                if not self._ui.confirm(
                    f"Continue without a backup of the volume {volume.name}? "
                    "(If something goes wrong its data cannot be restored.)",
                    title="Backup",
                    default=False,
                ):
                    raise UserAbort("Without a backup the operation stops.") from exc
        return created

    # ----------------------------------------------------------------------- restore

    def restore_all(
        self,
        items: Sequence[tuple[ResolvedVolume, Path]],
        image: str,
        *,
        app_name: str,
        backups_dir: Path,
        now: datetime,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        """Replace the contents of each volume with its archive.

        The application name must be typed in full. A safety copy of every volume is made
        first, then ``prepare`` (e.g. stop the application) runs, then the volumes are restored.
        """
        if not items:
            return
        names = ", ".join(volume.name for volume, _ in items)
        typed = self._ui.text(
            f"The contents of the volume(s) {names} will be OVERWRITTEN with the backup. "
            f"To continue, type the application name: {app_name}",
            title="Restore the volumes",
        )
        if typed.strip() != app_name:
            raise UserAbort("The name does not match; the restore was cancelled.")
        for volume, _ in items:
            try:
                self.archive(volume, image, PRE_RESTORE, backups_dir, now)
            except (VolumeBackupError, DockerError) as exc:
                raise VolumeBackupError(
                    f"The safety copy of the volume {volume.name} failed ({exc}); "
                    "nothing was restored."
                ) from exc
        if prepare is not None:
            prepare()
        for volume, archive in items:
            self._restore_one(volume, image, archive, backups_dir)
            self._log.info(f"The volume {volume.name} was restored from {archive.name}.")

    def _restore_one(self, volume: ResolvedVolume, image: str, archive: Path, safety: Path) -> None:
        try:
            _read_back(archive)
            result = self._docker.run_helper(
                image,
                RESTORE_COMMAND,
                mounts=[HelperMount(volume.source, DATA_MOUNT)],
                stdin_from=archive,
            )
        except DockerError as exc:
            raise VolumeBackupError(str(exc)) from exc
        if result.returncode != 0:
            raise VolumeBackupError(
                f"Restoring the volume {volume.name} failed: "
                f"{_tail(result.stderr) or 'tar error'}. The state before the restore "
                f"was saved in: {safety}"
            )

    # -------------------------------------------------------------------------- size

    def size_bytes(self, volume: ResolvedVolume, image: str) -> int | None:
        """The size of the volume's contents, or ``None`` if it cannot be measured."""
        return measure_volume(self._docker, volume, image)


def measure_volume(docker: Docker, volume: ResolvedVolume, image: str) -> int | None:
    """The size of a volume's contents in bytes (``du``), or ``None`` if unknown."""
    try:
        if not docker.image_exists(image):
            return None
        result = docker.run_helper(
            image,
            SIZE_COMMAND,
            mounts=[HelperMount(volume.source, DATA_MOUNT, read_only=True)],
            timeout=300,
        )
    except DockerError:
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.decode("utf-8", errors="replace").split()
    if not first or not first[0].isdigit():
        return None
    return int(first[0]) * 1024
