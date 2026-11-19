import stat
import sys
import tempfile
import unittest
from pathlib import Path

from telepito.composediff import FLAG, has_build_key, review_compose
from telepito.envfile import (
    EnvFileError,
    generate_secret,
    parse_env,
    read_env,
    render_assignment,
    update_env_file,
    update_text,
)

OLD = """services:
  app:
    image: investment:${APP_VERSION}
    env_file: .env.production
    ports:
      - "127.0.0.1:${PORT}:${PORT}"
    restart: unless-stopped
"""


class EnvQuotingTests(unittest.TestCase):
    def test_quotes_special_characters_literally(self) -> None:
        self.assertEqual(render_assignment("A", "p$ss #x = y\\z"), "A='p$ss #x = y\\z'")

    def test_rejects_unsafe_values_and_keys(self) -> None:
        for value in ["it's", "a\nb", "a\rb", "a\x00b"]:
            with self.subTest(value=value), self.assertRaises(EnvFileError):
                render_assignment("A", value)
        with self.assertRaises(EnvFileError):
            render_assignment("1BAD", "x")

    def test_generated_secret_never_needs_quoting(self) -> None:
        for _ in range(50):
            secret = generate_secret(48)
            self.assertRegex(secret, r"^[A-Za-z0-9_-]+$")
            self.assertGreaterEqual(len(secret), 48)

    def test_roundtrip_through_parse(self) -> None:
        text = "\n".join(render_assignment(k, v) for k, v in {"A": "x y", "B": "$HOME=#"}.items())
        self.assertEqual(parse_env(text), {"A": "x y", "B": "$HOME=#"})

    def test_parse_tolerates_comments_blank_and_foreign_styles(self) -> None:
        text = '# c\n\nexport A=1\nB="two"\nnot a line\n'
        self.assertEqual(parse_env(text), {"A": "1", "B": "two"})


class EnvUpdateTests(unittest.TestCase):
    def test_preserves_order_comments_and_foreign_lines(self) -> None:
        text = "# header\nA='1'\nCUSTOM=whatever   # mine\nB='2'\n"
        result = update_text(text, {"B": "changed", "C": "new"})
        self.assertEqual(
            result, "# header\nA='1'\nCUSTOM=whatever   # mine\nB='changed'\nC='new'\n"
        )

    def test_remove_and_duplicate_handling(self) -> None:
        text = "A='1'\nB='2'\nA='again'\n"
        self.assertEqual(update_text(text, {"A": "9"}, remove=["B"]), "A='9'\n")

    def test_empty_file(self) -> None:
        self.assertEqual(update_text("", {}), "")
        self.assertEqual(update_text("", {"A": "1"}), "A='1'\n")

    def test_invalid_value_leaves_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            update_env_file(path, {"A": "1"})
            with self.assertRaises(EnvFileError):
                update_env_file(path, {"B": "bad'value"})
            self.assertEqual(read_env(path), {"A": "1"})

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes")
    def test_file_is_created_600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            update_env_file(path, {"A": "1"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_file_reads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_env(Path(tmp) / "nope"), {})


class ComposeReviewTests(unittest.TestCase):
    def test_new_install_has_no_diff(self) -> None:
        review = review_compose(None, OLD)
        self.assertTrue(review.identical)
        self.assertFalse(review.build_present)

    def test_identical_files(self) -> None:
        review = review_compose(OLD, OLD)
        self.assertTrue(review.identical)
        self.assertFalse(review.needs_confirmation)

    def test_unchanged_risky_lines_are_not_flagged(self) -> None:
        new = OLD.replace("unless-stopped", "always")
        review = review_compose(OLD, new)
        self.assertFalse(review.identical)
        self.assertFalse(review.needs_confirmation)  # `ports:` is unchanged context
        self.assertNotIn(FLAG, review.diff)

    def test_added_risky_lines_are_flagged(self) -> None:
        sock = "/var/run/docker.sock"
        new = OLD + f"    volumes:\n      - {sock}:{sock}\n    privileged: true\n"
        review = review_compose(OLD, new)
        self.assertTrue(review.needs_confirmation)
        self.assertEqual(len(review.flagged), 3)
        self.assertIn(f"{FLAG} +    privileged: true", review.diff)

    def test_modified_risky_line_is_flagged_with_new_content(self) -> None:
        new = OLD.replace('"127.0.0.1:${PORT}:${PORT}"', '"0.0.0.0:${PORT}:${PORT}"')
        review = review_compose(OLD, new)
        self.assertEqual(review.flagged, ('- "0.0.0.0:${PORT}:${PORT}"',))

    def test_added_loopback_port_is_not_flagged_but_public_ones_are(self) -> None:
        base = "services:\n  app:\n    ports:\n"
        loop = base + '      - "127.0.0.1:${PORT}:${PORT}"\n'
        for public in ['"8080:80"', "0.0.0.0:80:80", '"${PORT}:${PORT}"', '"[::]:80:80"']:
            with self.subTest(public=public):
                new = loop + f"      - {public}\n"
                self.assertEqual(len(review_compose(loop, new).flagged), 1)
        self.assertFalse(review_compose(base, loop).needs_confirmation)

    def test_removed_risky_line_is_not_flagged(self) -> None:
        review = review_compose(OLD + "    privileged: true\n", OLD)
        self.assertFalse(review.needs_confirmation)

    def test_keyword_must_be_a_whole_word(self) -> None:
        new = OLD + "    labels:\n      - superuser_note=1\n      - myports=2\n"
        self.assertFalse(review_compose(OLD, new).needs_confirmation)

    def test_build_is_detected_anywhere_in_new_file(self) -> None:
        for snippet in ["    build: .\n", "    build:\n      context: .\n", "  - build: x\n"]:
            with self.subTest(snippet=snippet):
                self.assertTrue(review_compose(None, OLD + snippet).build_present)
                self.assertTrue(review_compose(OLD, OLD + snippet).build_present)

    def test_build_in_comment_or_other_word_is_ignored(self) -> None:
        self.assertFalse(has_build_key("# build: later\nrebuild: yes\nbuilder: x\n"))


if __name__ == "__main__":
    unittest.main()
