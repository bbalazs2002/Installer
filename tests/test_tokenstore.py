import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer.paths import TOKEN_FILE_ENV, default_paths
from installer.tokenstore import TokenStore


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.new = self.dir / "installer" / "token"
        self.old = self.dir / "telepito" / "token"
        self.old.parent.mkdir()

    def test_save_and_load(self) -> None:
        store = TokenStore(self.new)
        self.assertIsNone(store.load())
        store.save("github_pat_ABC123")
        self.assertEqual(store.load(), "github_pat_ABC123")
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.new.stat().st_mode), 0o600)

    def test_a_token_in_the_old_location_is_still_read(self) -> None:
        self.old.write_text("github_pat_OLD\n", encoding="utf-8")
        self.assertEqual(TokenStore(self.new, self.old).load(), "github_pat_OLD")

    def test_the_new_location_wins_and_saving_never_writes_the_old_one(self) -> None:
        self.old.write_text("github_pat_OLD\n", encoding="utf-8")
        store = TokenStore(self.new, self.old)
        store.save("github_pat_NEW")
        self.assertEqual(store.load(), "github_pat_NEW")
        self.assertEqual(self.old.read_text(encoding="utf-8").strip(), "github_pat_OLD")

    def test_delete_removes_both_so_no_stale_token_survives(self) -> None:
        self.old.write_text("github_pat_OLD\n", encoding="utf-8")
        store = TokenStore(self.new, self.old)
        store.save("github_pat_NEW")
        store.delete()
        self.assertIsNone(store.load())
        self.assertFalse(self.old.exists() or self.new.exists())

    def test_default_paths_know_the_old_location_only_for_the_default_token(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TOKEN_FILE_ENV, None)
            default = default_paths("/state")
            self.assertEqual(
                default.legacy_token_file, Path.home() / ".config" / "telepito" / "token"
            )
            self.assertIsNone(default_paths("/state", self.dir / "custom").legacy_token_file)
            os.environ[TOKEN_FILE_ENV] = str(self.dir / "from-env")
            self.assertIsNone(default_paths("/state").legacy_token_file)


if __name__ == "__main__":
    unittest.main()
