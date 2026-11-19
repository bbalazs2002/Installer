import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telepito.backup import (
    PRE_RESTORE,
    backup_filename,
    backup_refs,
    create_private_file,
    delete_backups,
    list_backups,
    offer_retention,
    old_backups,
)
from telepito.logging_ import Logger
from telepito.ui.scripted import ScriptedUi

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
        self.assertTrue(any("kihagyva" in line for line in self.log.records))

    def test_every_deletion_is_logged(self) -> None:
        self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800)
        delete_backups(self.dir, list_backups(self.dir), self.log)
        self.assertTrue(any("törölve: investment-0.1.0" in line for line in self.log.records))

    def test_nothing_old_means_no_question(self) -> None:
        self.make("investment-0.1.0-20261101T000000Z.dump", age_days=5)
        ui = ScriptedUi()
        flag = offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW)
        self.assertFalse(flag)
        self.assertEqual(ui.calls, [])

    def old_setup(self) -> Path:
        return self.make("investment-0.1.0-20200101T000000Z.dump", age_days=800, size=3 * 2**20)

    def test_question_shows_count_and_size(self) -> None:
        self.old_setup()
        ui = ScriptedUi(["later"])
        offer_retention(ui, self.log, self.dir, auto_delete=False, now=NOW)
        text = ui.calls[0].text
        self.assertIn("1 mentés", text)
        self.assertIn("3,0 MB", text)
        self.assertIn("egy évnél régebbi", text)

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


if __name__ == "__main__":
    unittest.main()
