import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from installer.backup import (
    PRE_RESTORE,
    backup_filename,
    backup_refs,
    create_private_file,
    delete_backups,
    group_sets,
    list_all_backups,
    list_backups,
    list_volume_backups,
    offer_retention,
    old_backups,
    volume_backup_filename,
)
from installer.logging_ import Logger
from installer.ui.scripted import ScriptedUi

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log = Logger(None)

    def make(self, name: str, *, age_days: int = 0, size: int = 10) -> Path:
        path = self.dir / name
        path.write_bytes(b"x" * size)
        stamp = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path


class NamingTests(BackupTestCase):
    def test_filename(self) -> None:
        self.assertEqual(
            backup_filename("investment", "0.2.0-rc.1", NOW),
            "investment-0.2.0-rc.1-20261119T120000Z.dump",
        )

    def test_private_file_is_created_empty_and_exclusive(self) -> None:
        path = self.dir / "a.dump"
        create_private_file(path)
        self.assertEqual(path.read_bytes(), b"")
        with self.assertRaises(FileExistsError):
            create_private_file(path)

    def test_listing_parses_and_ignores_everything_else(self) -> None:
        self.make("investment-0.2.0-20261101T000000Z.dump")
        self.make("investment-1.0.0-rc.1-20261102T000000Z.dump")
        self.make("investment-pre-restore-20261103T000000Z.dump")
        self.make("other_db-0.1.0-20261104T000000Z.dump")
        for junk in ["notes.txt", "investment-0.2.0.dump", "investment-abc-20261101T000000Z.dump",
                     "Investment-0.2.0-20261101T000000Z.dump", "x.dump.bak"]:  # fmt: skip
            self.make(junk)
        everything = list_backups(self.dir)
        self.assertEqual(
            [(f.database, f.version) for f in everything],
            [
                ("other_db", "0.1.0"),
                ("investment", "pre-restore"),
                ("investment", "1.0.0-rc.1"),
                ("investment", "0.2.0"),
            ],
        )
        self.assertEqual(len(list_backups(self.dir, "investment")), 3)

    def test_refs_exclude_pre_restore_safety_copies(self) -> None:
        self.make("investment-0.2.0-20261101T000000Z.dump")
        self.make("investment-pre-restore-20261103T000000Z.dump")
        refs = backup_refs(self.dir, "investment")
        self.assertEqual([r.version for r in refs], ["0.2.0"])
        self.assertTrue(refs[0].path.endswith("investment-0.2.0-20261101T000000Z.dump"))

    def test_missing_directory(self) -> None:
        self.assertEqual(list_backups(self.dir / "nope"), [])


class RetentionTests(BackupTestCase):
    def test_boundary_is_365_days_by_modification_time(self) -> None:
        self.make("investment-0.1.0-20250101T000000Z.dump", age_days=366)
        self.make("investment-0.2.0-20250102T000000Z.dump", age_days=364)
        # An old *name* but a fresh modification time is not old.
        self.make("investment-0.3.0-20200101T000000Z.dump", age_days=1)
        old = old_backups(list_backups(self.dir), NOW)
        self.assertEqual([f.version for f in old], ["0.1.0"])

    def test_delete_only_touches_valid_backups_in_the_directory(self) -> None:
        good = self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800)
        stranger = self.make("keep-me.txt", age_days=800)
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name) / "investment-0.1.0-20200101T000000Z.dump"
        outside.write_bytes(b"precious")
        files = list_backups(self.dir)
        forged = files[0].__class__(
            path=outside,
            database="investment",
            version="0.1.0",
            created="2020-01-01T00:00:00Z",
            size=8,
            modified=files[0].modified,
        )
        removed = delete_backups(self.dir, [*files, forged], self.log)
        self.assertEqual(removed, 1)
        self.assertFalse(good.exists())
        self.assertTrue(stranger.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(any("skipped" in line for line in self.log.records))

    def test_every_deletion_is_logged(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800)
        delete_backups(self.dir, list_backups(self.dir), self.log)  # explicit, not by the rule
        self.assertTrue(any("deleted: investment-0.1.0" in line for line in self.log.records))

    def test_nothing_old_means_no_question(self) -> None:
        self.make("investment-0.1.0-20261101T000000Z.dump", age_days=5)
        ui = ScriptedUi()
        flag = offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW)
        self.assertFalse(flag)
        self.assertEqual(ui.calls, [])

    def old_setup(self) -> Path:
        # The newest dump is always kept, so an old one only counts as old next to a newer one.
        self.make("investment-0.9.0-20261101T000000Z.dump", age_days=5)
        return self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800, size=3 * 2**20)

    def test_question_shows_count_and_size(self) -> None:
        self.old_setup()
        ui = ScriptedUi(["later"])
        offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW)
        text = ui.calls[0].text
        self.assertIn("1 backup(s)", text)
        self.assertIn("3.0 MB", text)
        self.assertIn("older than a year", text)

    def test_choices(self) -> None:
        path = self.old_setup()
        ui = ScriptedUi(["later"])
        self.assertFalse(offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW))
        self.assertTrue(path.exists())

        ui = ScriptedUi(["now"])
        self.assertFalse(offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW))
        self.assertFalse(path.exists())

        path = self.old_setup()
        ui = ScriptedUi(["always"])
        self.assertTrue(offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW))
        self.assertFalse(path.exists())

    def test_auto_delete_is_silent_unless_forced(self) -> None:
        path = self.old_setup()
        ui = ScriptedUi()
        self.assertTrue(offer_retention(ui, self.log, self.dir, auto_delete=True, now=NOW))
        self.assertFalse(path.exists())
        self.assertEqual(ui.calls, [])

        self.old_setup()
        ui = ScriptedUi(["later"])
        flag = offer_retention(ui, self.log, self.dir, auto_delete=True, now=NOW, force_prompt=True)
        self.assertTrue(flag)  # asking again does not switch the setting off
        self.assertEqual(len(ui.calls), 1)

    def test_database_filter(self) -> None:
        self.make("investment-0.2.0-20261101T000000Z.dump", age_days=5)
        mine = self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800)
        other = self.make("other-0.1.0-20200101T000000Z.dump", age_days=800)
        offer_retention(
            ScriptedUi(["now"]),
            self.log,
            self.dir,
            auto_delete=False,
            now=NOW,
            database="investment",
        )
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    def test_constant(self) -> None:
        self.assertEqual(PRE_RESTORE, "pre-restore")


class VolumeArchiveTests(BackupTestCase):
    def test_filename(self) -> None:
        self.assertEqual(
            volume_backup_filename("uploads", "0.2.0-rc.1", NOW),
            "uploads-0.2.0-rc.1-20261119T120000Z.tar.gz",
        )

    def test_listing_keeps_dumps_and_archives_apart(self) -> None:
        self.make("investment-0.2.0-20261101T000000Z.dump")
        self.make("uploads-0.2.0-20261101T000000Z.tar.gz")
        self.make("uploads-pre-restore-20261102T000000Z.tar.gz")
        for junk in ["uploads-0.2.0.tar.gz", "Uploads-0.2.0-20261101T000000Z.tar.gz",
                     "uploads-0.2.0-20261101T000000Z.tar"]:  # fmt: skip
            self.make(junk)
        self.assertEqual([f.database for f in list_backups(self.dir)], ["investment"])
        archives = list_volume_backups(self.dir)
        self.assertEqual([(f.database, f.version) for f in archives],
                         [("uploads", "pre-restore"), ("uploads", "0.2.0")])  # fmt: skip
        self.assertTrue(all(f.kind == "volume" for f in archives))
        everything = list_all_backups(self.dir, "investment")
        self.assertEqual(len(everything), 3)
        self.assertEqual(len(list_all_backups(self.dir, "other")), 2)  # no dump of "other"

    def test_a_volume_and_a_database_with_the_same_name_are_separate_series(self) -> None:
        self.make("data-0.1.0-20200101T000000Z.dump", age_days=800)
        self.make("data-0.1.0-20200101T000000Z.tar.gz", age_days=800)
        files = list_all_backups(self.dir)
        self.assertEqual(sorted(f.series for f in files), ["db:data", "volume:data"])
        self.assertEqual(old_backups(files, NOW), [])  # each is the newest of its own series

    def test_sets_group_the_files_of_one_run(self) -> None:
        self.make("investment-0.2.0-20261101T000000Z.dump", size=2**20)
        self.make("uploads-0.2.0-20261101T000000Z.tar.gz", size=2**20)
        self.make("media-0.2.0-20261101T000000Z.tar.gz", size=2**20)
        self.make("investment-0.1.0-20261001T000000Z.dump")
        sets = group_sets(list_all_backups(self.dir))
        self.assertEqual([(s.version, len(s.files)) for s in sets], [("0.2.0", 3), ("0.1.0", 1)])
        self.assertEqual(sets[0].size, 3 * 2**20)
        self.assertIsNotNone(sets[0].part("volume:uploads"))
        self.assertIsNone(sets[1].part("volume:uploads"))
        self.assertIn("uploads", sets[0].label())

    def test_delete_accepts_archives_and_nothing_else(self) -> None:
        archive = self.make("uploads-0.1.0-20200101T000000Z.tar.gz", age_days=800)
        stranger = self.make("uploads.tar.gz", age_days=800)
        removed = delete_backups(self.dir, list_volume_backups(self.dir), self.log)
        self.assertEqual(removed, 1)
        self.assertFalse(archive.exists())
        self.assertTrue(stranger.exists())


class NewestIsAlwaysKeptTests(BackupTestCase):
    """Older than a year is deleted, except the newest file of each series."""

    def test_a_single_old_backup_stays(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=2000)
        self.assertEqual(old_backups(list_all_backups(self.dir), NOW), [])

    def test_only_the_newest_of_the_old_ones_stays(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=2000)
        self.make("investment-0.2.0-20210101T000000Z.dump", age_days=1500)
        self.make("investment-0.3.0-20220101T000000Z.dump", age_days=1000)
        old = old_backups(list_all_backups(self.dir), NOW)
        self.assertEqual(sorted(f.version for f in old), ["0.1.0", "0.2.0"])

    def test_every_series_keeps_its_own_newest(self) -> None:
        for name in (
            "investment-0.1.0-20200101T000000Z.dump",
            "investment-0.2.0-20210101T000000Z.dump",
            "uploads-0.1.0-20200101T000000Z.tar.gz",
            "uploads-0.2.0-20210101T000000Z.tar.gz",
            "media-0.1.0-20190101T000000Z.tar.gz",
        ):
            self.make(name, age_days=1500)
        old = {f.path.name for f in old_backups(list_all_backups(self.dir), NOW)}
        self.assertEqual(
            old,
            {"investment-0.1.0-20200101T000000Z.dump", "uploads-0.1.0-20200101T000000Z.tar.gz"},
        )

    def test_a_fresh_newest_lets_all_the_old_ones_go(self) -> None:
        self.make("uploads-0.1.0-20200101T000000Z.tar.gz", age_days=900)
        self.make("uploads-0.2.0-20261110T000000Z.tar.gz", age_days=9)
        old = old_backups(list_all_backups(self.dir), NOW)
        self.assertEqual([f.version for f in old], ["0.1.0"])

    def test_a_set_expires_together(self) -> None:
        for stamp, age in (("20200101T000000Z", 1500), ("20261110T000000Z", 9)):
            self.make(f"investment-0.1.0-{stamp}.dump", age_days=age)
            self.make(f"uploads-0.1.0-{stamp}.tar.gz", age_days=age)
        old = {f.path.name for f in old_backups(list_all_backups(self.dir), NOW)}
        self.assertEqual(
            old, {"investment-0.1.0-20200101T000000Z.dump", "uploads-0.1.0-20200101T000000Z.tar.gz"}
        )

    def test_offering_deletes_archives_with_the_dumps(self) -> None:
        old_dump = self.make("investment-0.1.0-20200101T000000Z.dump", age_days=900)
        old_tar = self.make("uploads-0.1.0-20200101T000000Z.tar.gz", age_days=900)
        new_dump = self.make("investment-0.2.0-20261110T000000Z.dump", age_days=9)
        new_tar = self.make("uploads-0.2.0-20261110T000000Z.tar.gz", age_days=9)
        offer_retention(ScriptedUi(["now"]), self.log, self.dir, auto_delete=False, now=NOW)
        self.assertFalse(old_dump.exists() or old_tar.exists())
        self.assertTrue(new_dump.exists() and new_tar.exists())

    def test_the_question_says_what_is_kept(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=900)
        self.make("investment-0.2.0-20210101T000000Z.dump", age_days=800)
        ui = ScriptedUi(["later"])
        offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW)
        self.assertIn("newest backup", ui.calls[0].text)

    def test_nothing_is_asked_when_only_the_protected_file_is_old(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=900)
        ui = ScriptedUi()
        self.assertFalse(offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW))
        self.assertEqual(ui.calls, [])


if __name__ == "__main__":
    unittest.main()
