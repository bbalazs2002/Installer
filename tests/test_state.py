import json
import tempfile
import unittest
from pathlib import Path

from installer.fsutil import atomic_write_text
from installer.state import (
    DatabaseInfo,
    InstallInfo,
    InstallRecord,
    KnownRepo,
    StateError,
    find_install,
    is_effectively_empty,
    load_install_info,
    load_installs,
    load_repos,
    save_install_info,
    save_installs,
    save_repos,
    upsert_install,
)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class AtomicWriteTests(TempDirCase):
    def test_write_and_overwrite(self) -> None:
        path = self.dir / "f.txt"
        atomic_write_text(path, "one")
        atomic_write_text(path, "two")
        self.assertEqual(path.read_text(encoding="utf-8"), "two")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["f.txt"])

    def test_failed_write_leaves_no_temp_file(self) -> None:
        # Replacing a non-empty directory fails after the temp file was created.
        target = self.dir / "target"
        target.mkdir()
        (target / "inner").write_text("x", encoding="utf-8")
        with self.assertRaises(OSError):
            atomic_write_text(target, "data")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["target"])


class ReposTests(TempDirCase):
    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(load_repos(self.dir / "repos.json"), [])

    def test_roundtrip(self) -> None:
        path = self.dir / "repos.json"
        repos = [KnownRepo("bbalazs2002/Investment", "/var/www/investment"), KnownRepo("a/b")]
        save_repos(path, repos)
        self.assertEqual(load_repos(path), repos)

    def test_invalid_repo_rejected(self) -> None:
        path = self.dir / "repos.json"
        path.write_text(
            json.dumps({"schemaVersion": 1, "repos": [{"repo": "../x"}]}), encoding="utf-8"
        )
        with self.assertRaises(StateError):
            load_repos(path)


class InstallsTests(TempDirCase):
    def record(self, name: str = "investment", version: str = "0.1.0") -> InstallRecord:
        return InstallRecord(
            name, "o/investment", "/var/www/investment", version, "2026-11-18T10:00:00Z"
        )

    def test_roundtrip_and_upsert(self) -> None:
        path = self.dir / "installs.json"
        self.assertEqual(load_installs(path), [])
        records = upsert_install([], self.record())
        records = upsert_install(records, self.record("alpha"))
        records = upsert_install(records, self.record(version="0.2.0"))
        save_installs(path, records)
        loaded = load_installs(path)
        self.assertEqual([r.name for r in loaded], ["alpha", "investment"])
        found = find_install(loaded, "investment")
        assert found is not None
        self.assertEqual(found.version, "0.2.0")
        self.assertIsNone(find_install(loaded, "zzz"))

    def test_corrupt_files_raise(self) -> None:
        path = self.dir / "installs.json"
        for text in ["{", "[]", '{"schemaVersion": "1"}', '{"schemaVersion": 99}']:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(StateError):
                load_installs(path)


class InstallInfoTests(TempDirCase):
    def info(self) -> InstallInfo:
        return InstallInfo(
            name="investment",
            repo="o/investment",
            version="0.2.0",
            installed_at="2026-11-18T10:00:00Z",
            previous_versions=("0.1.0",),
            migrations=("20260101_init", "20260201_x"),
            database=DatabaseInfo(
                container="database-postgres-1",
                network="shared-infra",
                host="postgres",
                name="investment",
                user="investment",
            ),
            port=2570,
            auto_delete_old_backups=True,
        )

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(load_install_info(self.dir))

    def test_roundtrip(self) -> None:
        save_install_info(self.dir, self.info())
        self.assertEqual(load_install_info(self.dir), self.info())

    def test_without_database(self) -> None:
        info = InstallInfo("app", "o/app", "1.0.0", "2026-11-18T10:00:00Z")
        save_install_info(self.dir, info)
        loaded = load_install_info(self.dir)
        assert loaded is not None
        self.assertIsNone(loaded.database)
        self.assertEqual(loaded.migrations, ())

    def test_never_stores_a_password(self) -> None:
        save_install_info(self.dir, self.info())
        raw = (self.dir / ".install.json").read_text(encoding="utf-8")
        self.assertNotIn("password", raw.lower())

    def test_unknown_keys_are_dropped_on_rewrite(self) -> None:
        save_install_info(self.dir, self.info())
        path = self.dir / ".install.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["database"]["password"] = "hunter2"
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_install_info(self.dir)
        assert loaded is not None
        save_install_info(self.dir, loaded)
        self.assertNotIn("hunter2", path.read_text(encoding="utf-8"))

    def test_unrecognised_file_raises(self) -> None:
        path = self.dir / ".install.json"
        bad_files = [
            "",
            "not json",
            "[]",
            '{"schemaVersion": 1}',
            '{"schemaVersion": 5, "name": "x"}',
        ]
        for text in bad_files:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(StateError):
                load_install_info(self.dir)

    def test_is_effectively_empty(self) -> None:
        self.assertTrue(is_effectively_empty(self.dir / "missing"))
        self.assertTrue(is_effectively_empty(self.dir))
        (self.dir / "x").write_text("1", encoding="utf-8")
        self.assertFalse(is_effectively_empty(self.dir))


if __name__ == "__main__":
    unittest.main()
