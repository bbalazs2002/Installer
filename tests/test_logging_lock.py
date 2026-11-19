import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telepito.fsutil import atomic_symlink
from telepito.lock import FileLock, LockBusy
from telepito.logging_ import Logger, Redactor


class RedactorTests(unittest.TestCase):
    def test_masks_registered_secrets_everywhere(self) -> None:
        r = Redactor()
        r.register("hunter2-secret")
        r.register("tok_ABCDEFGH")
        text = "pw=hunter2-secret url=https://x/?t=tok_ABCDEFGH ok"
        self.assertEqual(r.redact(text), "pw=*** url=https://x/?t=*** ok")

    def test_longest_secret_masked_first(self) -> None:
        r = Redactor()
        r.register("abcd")
        r.register("abcdEFGH")
        self.assertEqual(r.redact("x abcdEFGH y"), "x *** y")

    def test_ignores_empty_and_tiny_secrets(self) -> None:
        r = Redactor()
        r.register("")
        r.register(None)
        r.register("ab")
        self.assertEqual(r.redact("ab cd"), "ab cd")


class LoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "logs"

    def test_writes_masked_daily_file(self) -> None:
        r = Redactor()
        r.register("s3cr3t-value")
        fixed = datetime(2026, 11, 19, 8, 30, tzinfo=timezone.utc)
        log = Logger(self.dir, r, clock=lambda: fixed)
        log.info("jelszó: s3cr3t-value")
        log.output("line one\nline two with s3cr3t-value")
        content = (self.dir / "2026-11-19.log").read_text(encoding="utf-8")
        self.assertNotIn("s3cr3t-value", content)
        self.assertIn("2026-11-19T08:30:00Z INFO  jelszó: ***", content)
        self.assertEqual(len(content.splitlines()), 3)

    def test_secret_registered_later_is_masked_from_then_on(self) -> None:
        log = Logger(None)
        log.info("before LATESECRET1")
        log.redactor.register("LATESECRET1")
        log.info("after LATESECRET1")
        self.assertIn("LATESECRET1", log.records[0])
        self.assertNotIn("LATESECRET1", log.records[1])

    def test_memory_mode_writes_no_files(self) -> None:
        Logger(None).info("x")
        self.assertFalse(self.dir.exists())


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".lock"

    def test_second_holder_is_refused(self) -> None:
        with FileLock(self.path), self.assertRaises(LockBusy):
            FileLock(self.path).acquire()

    def test_release_allows_reacquire(self) -> None:
        with FileLock(self.path):
            pass
        with FileLock(self.path):
            pass

    def test_release_without_acquire_is_harmless(self) -> None:
        FileLock(self.path).release()


class SymlinkTests(unittest.TestCase):
    def test_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a").mkdir()
            (base / "b").mkdir()
            link = base / "current"
            atomic_symlink(link, "a")
            self.assertEqual(link.resolve(), (base / "a").resolve())
            atomic_symlink(link, "b")
            self.assertEqual(link.resolve(), (base / "b").resolve())
            self.assertEqual(sorted(p.name for p in base.iterdir()), ["a", "b", "current"])


if __name__ == "__main__":
    unittest.main()
