import unittest
from datetime import datetime, timezone

from telepito import __version__
from telepito.cli import main
from telepito.docker import ContainerInfo, Docker
from telepito.github import Asset, GitHub, GitHubError, GitHubRateLimited, Release
from telepito.validators import (
    is_app_name,
    is_asset_name,
    is_db_identifier,
    is_env_name,
    is_repo,
)
from telepito.versions import Version


class InterfaceTests(unittest.TestCase):
    def test_interfaces_are_abstract(self) -> None:
        for cls in (Docker, GitHub):
            with self.subTest(cls=cls.__name__), self.assertRaises(TypeError):
                cls()  # type: ignore[abstract]

    def test_container_info_defaults(self) -> None:
        c = ContainerInfo(name="db", image="postgres:17-alpine")
        self.assertEqual(dict(c.env), {})
        self.assertEqual(dict(c.networks), {})

    def test_release_helpers(self) -> None:
        release = Release(
            tag="v1.2.0",
            name="1.2.0",
            prerelease=False,
            published_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
            body="",
            assets=(Asset(1, "deploy.json", 10), Asset(2, "SHA256SUMS", 5)),
        )
        self.assertEqual(release.version, Version(1, 2, 0))
        asset = release.asset("deploy.json")
        assert asset is not None
        self.assertEqual(asset.id, 1)
        self.assertIsNone(release.asset("image.tar.gz"))

    def test_non_semver_tag_has_no_version(self) -> None:
        release = Release("nightly", "", False, None, "", ())
        self.assertIsNone(release.version)

    def test_rate_limit_error(self) -> None:
        err = GitHubRateLimited("lassú", retry_after=30)
        self.assertIsInstance(err, GitHubError)
        self.assertEqual(err.retry_after, 30)


class ValidatorTests(unittest.TestCase):
    def test_repo(self) -> None:
        for ok in ["bbalazs2002/Install", "a/b", "o.w-n_er/re.po"]:
            self.assertTrue(is_repo(ok), ok)
        for bad in ["", "a", "a/b/c", "../x", "a/..", "./x", "a b/c", "a/b;ls", "/a"]:
            self.assertFalse(is_repo(bad), bad)

    def test_app_name(self) -> None:
        self.assertTrue(is_app_name("investment"))
        self.assertTrue(is_app_name("games-center"))
        for bad in ["", "Games", "1x", "-x", "a_b", "x" * 42]:
            self.assertFalse(is_app_name(bad), bad)

    def test_db_identifier(self) -> None:
        self.assertTrue(is_db_identifier("investment_2"))
        for bad in ["", "Inv", "1a", "a-b", 'a"; drop', "x" * 64]:
            self.assertFalse(is_db_identifier(bad), bad)

    def test_env_name_and_asset_name(self) -> None:
        self.assertTrue(is_env_name("DATABASE_URL"))
        self.assertFalse(is_env_name("1X"))
        self.assertFalse(is_env_name("A-B"))
        self.assertTrue(is_asset_name("image.tar.gz"))
        for bad in ["", "../a", "a/b", ".x", "a..b", "a\\b"]:
            self.assertFalse(is_asset_name(bad), bad)


class CliTests(unittest.TestCase):
    def test_version_flag(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertTrue(__version__)


if __name__ == "__main__":
    unittest.main()
