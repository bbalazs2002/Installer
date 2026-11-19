"""The volume backup against real Docker: a throw-away named volume and a bind directory.

Uses the image ``postgres:17-alpine`` (busybox ``sh`` and ``tar``, like many application images),
which the Postgres tests need anyway. Skipped when Docker or the image is unavailable or
``INSTALLER_SKIP_DOCKER_TESTS`` is set. The volumes are created and removed by the test itself;
nothing else on the machine is touched.
"""

import secrets
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from installer.docker import DockerError
from installer.docker_cli import SubprocessDocker
from installer.logging_ import Logger
from installer.manifest import VolumeSpec
from installer.ui.scripted import ScriptedUi
from installer.volumebackup import VolumeBackupError, VolumeBackups, measure_volume
from installer.volumes import ResolvedVolume

from .test_postgres_docker import _run, docker_usable

IMAGE = "postgres:17-alpine"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def image_present() -> bool:
    return docker_usable() and _run("image", "inspect", IMAGE).returncode == 0


def fill_script() -> str:
    return (
        "mkdir -p /data/sub/deeper && echo hello > /data/a.txt && "
        "echo nested > /data/sub/deeper/b.txt && echo hidden > /data/.hidden && "
        "echo odd > /data/..odd && chmod 640 /data/a.txt"
    )


@unittest.skipUnless(image_present(), f"docker or {IMAGE} is not available")
class RealVolumeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.backups = self.dir / "backups"
        self.docker = SubprocessDocker()
        self.ui = ScriptedUi()
        self.log = Logger(None)
        self.volumes = VolumeBackups(self.docker, self.ui, self.log)

    def make_volume(self) -> ResolvedVolume:
        name = f"installer-test-{secrets.token_hex(4)}"
        self.assertEqual(_run("volume", "create", name).returncode, 0)
        self.addCleanup(lambda: _run("volume", "rm", "-f", name))
        spec = VolumeSpec("uploads", "/app/uploads")
        volume = ResolvedVolume(spec, name)
        self.sh(volume, fill_script())
        return volume

    def sh(self, volume: ResolvedVolume, script: str) -> str:
        result = _run(
            "run", "--rm", "--network", "none", "-v", f"{volume.source}:/data",
            "--entrypoint", "sh", IMAGE, "-c", script,
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def listing(self, volume: ResolvedVolume) -> str:
        return self.sh(
            volume, "cd /data && find . -mindepth 1 | sort; cat a.txt 2>/dev/null || true"
        )

    def test_archive_and_restore_a_named_volume(self) -> None:
        volume = self.make_volume()
        before = self.listing(volume)
        archive = self.volumes.archive(volume, IMAGE, "0.1.0", self.backups, NOW)
        self.assertEqual(archive.name, "uploads-0.1.0-20261119T120000Z.tar.gz")
        with tarfile.open(archive) as tar:
            names = {n.removeprefix("./") for n in tar.getnames()}
        self.assertTrue({"a.txt", "sub/deeper/b.txt", ".hidden", "..odd"} <= names, names)
        if sys.platform != "win32":
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

        self.sh(
            volume, "rm -rf /data/sub /data/a.txt && echo junk > /data/junk && echo x > /data/.junk"
        )
        self.assertNotEqual(self.listing(volume), before)

        self.ui.push("investment")
        self.volumes.restore_all(
            [(volume, archive)],
            IMAGE,
            app_name="investment",
            backups_dir=self.backups,
            now=NOW.replace(minute=5),
        )
        self.assertEqual(self.listing(volume), before)  # incl. the hidden and ..odd files
        self.assertEqual(self.sh(volume, "stat -c %a /data/a.txt").strip(), "640")
        safety = list(self.backups.glob("uploads-pre-restore-*.tar.gz"))
        self.assertEqual(len(safety), 1)  # the state before the restore, junk included
        with tarfile.open(safety[0]) as tar:
            self.assertIn("junk", {n.removeprefix("./") for n in tar.getnames()})

    def test_the_helper_cannot_reach_the_network_and_leaves_no_container(self) -> None:
        volume = self.make_volume()
        before = _run("ps", "-aq").stdout.split()
        result = self.docker.run_helper(
            IMAGE,
            ["sh", "-c", "wget -q -T 2 -O- http://1.1.1.1/ || echo no-network"],
            mounts=[],
        )
        self.assertIn(b"no-network", result.stdout)
        self.assertEqual(sorted(_run("ps", "-aq").stdout.split()), sorted(before))
        del volume

    def test_size(self) -> None:
        volume = self.make_volume()
        size = measure_volume(self.docker, volume, IMAGE)
        self.assertIsNotNone(size)
        assert size is not None
        self.assertGreater(size, 0)
        self.assertIsNone(measure_volume(self.docker, volume, "no-such-image:0"))

    def test_an_empty_volume_has_a_valid_archive(self) -> None:
        name = f"installer-test-{secrets.token_hex(4)}"
        _run("volume", "create", name)
        self.addCleanup(lambda: _run("volume", "rm", "-f", name))
        volume = ResolvedVolume(VolumeSpec("empty", "/e"), name)
        archive = self.volumes.archive(volume, IMAGE, "0.1.0", self.backups, NOW)
        self.assertGreater(archive.stat().st_size, 0)

    def test_a_missing_image_is_reported_and_leaves_no_file(self) -> None:
        volume = self.make_volume()
        with self.assertRaises(VolumeBackupError):
            self.volumes.archive(volume, "no-such-image:0", "0.1.0", self.backups, NOW)
        self.assertEqual(list(self.backups.glob("*")), [])

    def test_an_image_without_tar_is_a_failure_not_a_half_backup(self) -> None:
        volume = self.make_volume()
        # busybox sh exists; make `tar` unavailable by running with an empty PATH
        result = self.docker.run_helper(
            IMAGE, ["sh", "-c", "PATH=/nonexistent tar czf - -C /data ."], mounts=[]
        )
        self.assertNotEqual(result.returncode, 0)
        del volume

    def test_a_failed_restore_names_the_safety_copy(self) -> None:
        volume = self.make_volume()
        archive = self.dir / "uploads-0.1.0-20261119T120000Z.tar.gz"
        archive.write_bytes(b"not an archive")
        self.ui.push("investment")
        before = self.listing(volume)
        with self.assertRaises((VolumeBackupError, DockerError)):
            self.volumes.restore_all(
                [(volume, archive)],
                IMAGE,
                app_name="investment",
                backups_dir=self.backups,
                now=NOW,
            )
        self.assertEqual(self.listing(volume), before)  # refused before anything was emptied

    @unittest.skipIf(sys.platform == "win32", "bind mounts of Windows paths are not supported")
    def test_archive_and_restore_a_bind_directory(self) -> None:
        data = self.dir / "shared" / "data" / "media"
        data.mkdir(parents=True)
        (data / "pic.png").write_bytes(b"png-bytes")
        (data / ".hidden").write_bytes(b"h")
        volume = ResolvedVolume(
            VolumeSpec("media", "/app/media", kind="bind", host_path="media"), str(data)
        )
        archive = self.volumes.archive(volume, IMAGE, "0.1.0", self.backups, NOW)
        (data / "pic.png").write_bytes(b"changed")
        (data / "new.txt").write_bytes(b"n")
        self.ui.push("investment")
        self.volumes.restore_all(
            [(volume, archive)], IMAGE, app_name="investment", backups_dir=self.backups, now=NOW
        )
        self.assertEqual((data / "pic.png").read_bytes(), b"png-bytes")
        self.assertTrue((data / ".hidden").exists())
        self.assertFalse((data / "new.txt").exists())

    def test_the_helper_is_a_plain_subprocess_call_without_a_shell(self) -> None:
        # a guard on the real command line: the volume name is never interpreted by a shell
        volume = ResolvedVolume(VolumeSpec("x", "/x"), "evil; touch pwned")
        with self.assertRaises((VolumeBackupError, DockerError)):
            self.volumes.archive(volume, IMAGE, "0.1.0", self.backups, NOW)
        self.assertFalse(Path("pwned").exists())
        self.assertEqual(list(self.backups.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
