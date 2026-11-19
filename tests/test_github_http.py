import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from installer.github import (
    Asset,
    GitHubAccessDenied,
    GitHubAuthError,
    GitHubError,
    GitHubRateLimited,
    GitHubUnavailable,
    TokenStatus,
)
from installer.github_http import (
    HttpGitHub,
    parse_token_expiry,
    token_expiry_warning,
)

from .fake_github import TOKEN, FakeGitHub, make_release


class Clock:
    """A controllable clock and a sleep that only records."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class GitHubTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGitHub()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock()
        self.client = self.make_client()

    def make_client(self, token: str = TOKEN, **kwargs: object) -> HttpGitHub:
        return HttpGitHub(
            token,
            api_base=self.fake.api_base,
            sleep=self.clock.sleep,
            clock=self.clock.time,
            connect_timeout=5,
            read_timeout=5,
            **kwargs,  # type: ignore[arg-type]
        )


class TokenTests(GitHubTestCase):
    def test_invalid_tokens_rejected_up_front(self) -> None:
        for bad in ["", "short", "has space in it", "tok\nen12345678", "tökén_with_accents_1"]:
            with self.subTest(token=bad), self.assertRaises(GitHubAuthError):
                HttpGitHub(bad)

    def test_repr_hides_token(self) -> None:
        self.assertNotIn(TOKEN, repr(self.client))

    def test_wrong_token_is_auth_error_without_leaking_it(self) -> None:
        client = self.make_client("github_pat_WRONGTOKEN99999")
        with self.assertRaises(GitHubAuthError) as ctx:
            client.check_access("o/r")
        self.assertNotIn("WRONGTOKEN", str(ctx.exception))
        self.assertEqual(len(self.fake.state.api_requests), 1)  # 401 is not retried

    def test_token_only_in_authorization_header(self) -> None:
        self.client.check_access("o/r")
        for request in self.fake.state.api_requests:
            self.assertNotIn(TOKEN, request.path)
            self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")

    def test_check_access_reads_expiry(self) -> None:
        status = self.client.check_access("o/r")
        self.assertEqual(status.expires_at, datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_check_access_without_expiry_header(self) -> None:
        self.fake.state.token_expiry = None
        self.assertIsNone(self.client.check_access("o/r").expires_at)

    def test_invalid_repo_never_hits_the_network(self) -> None:
        with self.assertRaises(GitHubError):
            self.client.check_access("../etc")
        self.assertEqual(self.fake.state.api_requests, [])


class ExpiryTests(unittest.TestCase):
    NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)

    def test_parse(self) -> None:
        self.assertEqual(
            parse_token_expiry("2026-12-18 12:30:00 UTC"),
            datetime(2026, 12, 18, 12, 30, tzinfo=timezone.utc),
        )
        self.assertIsNone(parse_token_expiry("garbage"))
        self.assertIsNone(parse_token_expiry(None))

    def test_warning_thresholds(self) -> None:
        def warn(delta: timedelta | None) -> str | None:
            expires = None if delta is None else self.NOW + delta
            return token_expiry_warning(TokenStatus(expires), self.NOW)

        self.assertIsNone(warn(None))
        self.assertIsNone(warn(timedelta(days=30)))
        self.assertIn("expires", warn(timedelta(days=13)) or "")
        self.assertIn("expires", warn(timedelta(days=14)) or "")
        self.assertIn("expired", warn(timedelta(seconds=-1)) or "")


class ReleaseListTests(GitHubTestCase):
    def test_drafts_hidden_and_sorted_newest_first(self) -> None:
        self.fake.state.releases = [
            make_release("v0.1.0"),
            make_release("v0.3.0-rc.1", prerelease=True),
            make_release("v0.2.0"),
            make_release("v9.9.9", draft=True),
            make_release("v0.10.0"),
            make_release("nightly"),
        ]
        releases = self.client.list_releases("o/r")
        self.assertEqual(
            [r.tag for r in releases],
            ["v0.10.0", "v0.3.0-rc.1", "v0.2.0", "v0.1.0", "nightly"],
        )
        self.assertTrue(releases[1].prerelease)

    def test_pagination_follows_link_header(self) -> None:
        self.fake.state.per_page_cap = 2
        self.fake.state.releases = [make_release(f"v1.0.{i}") for i in range(5)]
        tags = [r.tag for r in self.client.list_releases("o/r")]
        self.assertEqual(tags, [f"v1.0.{i}" for i in (4, 3, 2, 1, 0)])
        self.assertEqual(len(self.fake.state.api_requests), 3)

    def test_foreign_next_link_is_refused(self) -> None:
        self.fake.state.per_page_cap = 1
        self.fake.state.releases = [make_release("v1.0.0"), make_release("v1.0.1")]
        self.fake.state.api_base = "http://evil.example:9"  # makes the Link point away
        with self.assertRaises(GitHubError) as ctx:
            self.client.list_releases("o/r")
        self.assertIn("foreign", str(ctx.exception))
        self.assertEqual(len(self.fake.state.api_requests), 1)

    def test_latest_stable_skips_prereleases(self) -> None:
        self.fake.state.releases = [
            make_release("v2.0.0-beta.1", prerelease=True),
            make_release("v1.5.0"),
            make_release("v1.4.0"),
        ]
        latest = self.client.latest_stable("o/r")
        assert latest is not None
        self.assertEqual(latest.tag, "v1.5.0")

    def test_latest_stable_none(self) -> None:
        self.fake.state.releases = [make_release("v1.0.0-rc.1", prerelease=True)]
        self.assertIsNone(self.client.latest_stable("o/r"))

    def test_assets_and_dates_parsed(self) -> None:
        self.fake.state.releases = [
            make_release(
                "v1.0.0",
                asset_ids=((7, "deploy.json", 12),),
                published="2026-11-02T08:00:00Z",
                body="Changes",
            )
        ]
        (release,) = self.client.list_releases("o/r")
        self.assertEqual(release.assets, (Asset(7, "deploy.json", 12),))
        self.assertEqual(release.published_at, datetime(2026, 11, 2, 8, tzinfo=timezone.utc))
        self.assertEqual(release.body, "Changes")


class CacheTests(GitHubTestCase):
    def test_within_ttl_no_request(self) -> None:
        self.fake.state.releases = [make_release("v1.0.0")]
        self.client.list_releases("o/r")
        self.client.list_releases("o/r")
        self.assertEqual(len(self.fake.state.api_requests), 1)

    def test_after_ttl_revalidates_with_etag(self) -> None:
        self.fake.state.releases = [make_release("v1.0.0")]
        self.client.list_releases("o/r")
        self.clock.now += 601
        releases = self.client.list_releases("o/r")
        self.assertEqual([r.tag for r in releases], ["v1.0.0"])
        self.assertEqual(len(self.fake.state.api_requests), 2)
        self.assertEqual(self.fake.state.etag_hits, 1)
        self.assertIn("If-None-Match", self.fake.state.api_requests[1].headers)

    def test_changed_content_after_ttl_is_refetched(self) -> None:
        self.fake.state.releases = [make_release("v1.0.0")]
        self.client.list_releases("o/r")
        self.fake.state.releases.append(make_release("v1.1.0"))
        self.clock.now += 601
        self.assertEqual([r.tag for r in self.client.list_releases("o/r")], ["v1.1.0", "v1.0.0"])

    def test_check_access_is_never_served_from_cache(self) -> None:
        self.client.check_access("o/r")
        self.client.check_access("o/r")
        self.assertEqual(len(self.fake.state.api_requests), 2)


class ErrorHandlingTests(GitHubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake.state.releases = [make_release("v1.0.0")]

    def test_5xx_retried_with_exponential_backoff_then_succeeds(self) -> None:
        self.fake.state.failures.extend([(503, {}), (502, {})])
        self.assertEqual(len(self.client.list_releases("o/r")), 1)
        self.assertEqual(self.clock.sleeps, [0.5, 1.0])

    def test_5xx_gives_up_after_max_attempts(self) -> None:
        self.fake.state.failures.extend([(500, {})] * 10)
        with self.assertRaises(GitHubUnavailable):
            self.client.list_releases("o/r")
        self.assertEqual(len(self.fake.state.api_requests), 4)

    def test_connection_failure_is_unavailable(self) -> None:
        client = HttpGitHub(
            TOKEN,
            api_base="http://127.0.0.1:1",
            sleep=self.clock.sleep,
            clock=self.clock.time,
            connect_timeout=1,
            max_attempts=2,
        )
        with self.assertRaises(GitHubUnavailable):
            client.list_releases("o/r")
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_retry_after_is_honoured(self) -> None:
        self.fake.state.failures.append((429, {"Retry-After": "7"}))
        self.assertEqual(len(self.client.list_releases("o/r")), 1)
        self.assertEqual(self.clock.sleeps, [7.0])

    def test_403_with_retry_after_is_rate_limit(self) -> None:
        self.fake.state.failures.append((403, {"Retry-After": "3"}))
        self.client.list_releases("o/r")
        self.assertEqual(self.clock.sleeps, [3.0])

    def test_long_retry_after_is_reported_not_waited(self) -> None:
        self.fake.state.failures.append((429, {"Retry-After": "3600"}))
        with self.assertRaises(GitHubRateLimited) as ctx:
            self.client.list_releases("o/r")
        self.assertEqual(ctx.exception.retry_after, 3600)
        self.assertEqual(self.clock.sleeps, [])

    def test_exhausted_quota_uses_reset_header(self) -> None:
        reset = str(int(self.clock.now) + 5)
        self.fake.state.failures.append(
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
        )
        self.client.list_releases("o/r")
        self.assertEqual(self.clock.sleeps, [5.0])

    def test_plain_403_and_404_mean_no_access(self) -> None:
        for status in (403, 404):
            self.fake.state.failures.append((status, {}))
            with self.subTest(status=status), self.assertRaises(GitHubAccessDenied):
                self.client.list_releases("o/r")
        self.assertEqual(self.clock.sleeps, [])

    def test_unexpected_status(self) -> None:
        self.fake.state.failures.append((418, {}))
        with self.assertRaises(GitHubError) as ctx:
            self.client.list_releases("o/r")
        self.assertIn("418", str(ctx.exception))


class DownloadTests(GitHubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.payload = bytes(range(256)) * 2000  # 512 000 bytes
        self.fake.state.assets[11] = self.payload
        self.asset = Asset(11, "image.tar.gz", len(self.payload))

    def test_download_follows_redirect_and_drops_authorization(self) -> None:
        dest = self.dir / "image.tar.gz"
        calls: list[tuple[int, int]] = []
        self.client.download_asset("o/r", self.asset, dest, lambda d, t: calls.append((d, t)))
        self.assertEqual(
            hashlib.sha256(dest.read_bytes()).hexdigest(), hashlib.sha256(self.payload).hexdigest()
        )
        # The API call carried the token and asked for the raw bytes ...
        api = self.fake.state.api_requests[0]
        self.assertEqual(api.headers["Accept"], "application/octet-stream")
        self.assertEqual(api.headers["Authorization"], f"Bearer {TOKEN}")
        # ... the storage host (a different port) never saw it.
        (storage,) = self.fake.state.storage_requests
        self.assertNotIn("Authorization", storage.headers)
        self.assertEqual(calls[-1], (len(self.payload), len(self.payload)))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["image.tar.gz"])

    def test_truncated_download_is_retried(self) -> None:
        self.fake.state.truncate_next_download = True
        dest = self.dir / "image.tar.gz"
        self.client.download_asset("o/r", self.asset, dest)
        self.assertEqual(dest.read_bytes(), self.payload)
        self.assertEqual(len(self.fake.state.storage_requests), 2)
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_failed_download_leaves_no_files(self) -> None:
        dest = self.dir / "image.tar.gz"
        with self.assertRaises(GitHubAccessDenied):
            self.client.download_asset("o/r", Asset(999, "missing", 1), dest)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_size_mismatch_with_asset_metadata_fails(self) -> None:
        dest = self.dir / "image.tar.gz"
        wrong = Asset(11, "image.tar.gz", len(self.payload) + 5)
        with self.assertRaises(GitHubUnavailable):
            self.client.download_asset("o/r", wrong, dest)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_existing_destination_untouched_on_failure(self) -> None:
        dest = self.dir / "image.tar.gz"
        dest.write_bytes(b"old")
        with self.assertRaises(GitHubAccessDenied):
            self.client.download_asset("o/r", Asset(999, "missing", 1), dest)
        self.assertEqual(dest.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
