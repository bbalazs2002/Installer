import unittest

from telepito.versions import Version, VersionError


class ParseTests(unittest.TestCase):
    def test_plain_and_v_prefix(self) -> None:
        self.assertEqual(Version.parse("1.2.3"), Version(1, 2, 3))
        self.assertEqual(Version.parse("v1.2.3"), Version(1, 2, 3))

    def test_prerelease_and_build(self) -> None:
        v = Version.parse("v1.0.0-rc.1+build.5")
        self.assertEqual(v.prerelease, ("rc", "1"))
        self.assertEqual(v.build, ("build", "5"))
        self.assertTrue(v.is_prerelease)

    def test_tag_forms(self) -> None:
        v = Version.parse("v1.2.0")
        self.assertEqual(v.tag, "v1.2.0")
        self.assertEqual(v.image_tag, "1.2.0")

    def test_invalid(self) -> None:
        for text in ["", "1", "1.2", "1.2.3.4", "01.2.3", "1.2.3-", "1.2.3-01", "a.b.c", "v", " "]:
            with self.subTest(text=text), self.assertRaises(VersionError):
                Version.parse(text)

    def test_non_string(self) -> None:
        with self.assertRaises(VersionError):
            Version.parse(None)  # type: ignore[arg-type]

    def test_try_parse(self) -> None:
        self.assertIsNone(Version.try_parse("nope"))
        self.assertEqual(Version.try_parse("2.0.0"), Version(2, 0, 0))


class OrderingTests(unittest.TestCase):
    def test_spec_precedence_chain(self) -> None:
        # The ordering example from the SemVer 2.0 specification.
        chain = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [Version.parse(t) for t in chain]
        self.assertEqual(sorted(reversed(versions)), versions)
        for lower, higher in zip(versions, versions[1:], strict=False):
            self.assertLess(lower, higher)
            self.assertGreater(higher, lower)

    def test_numeric_not_lexical(self) -> None:
        self.assertLess(Version.parse("1.2.9"), Version.parse("1.2.10"))
        self.assertLess(Version.parse("1.9.0"), Version.parse("1.10.0"))
        self.assertLess(Version.parse("2.0.0"), Version.parse("10.0.0"))

    def test_prerelease_below_release(self) -> None:
        self.assertLess(Version.parse("1.0.0-rc.1"), Version.parse("1.0.0"))
        self.assertLess(Version.parse("0.9.9"), Version.parse("1.0.0-alpha"))

    def test_build_metadata_ignored(self) -> None:
        a, b = Version.parse("1.0.0+a"), Version.parse("1.0.0+b")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertLessEqual(a, b)
        self.assertGreaterEqual(a, b)

    def test_str_roundtrip(self) -> None:
        for text in ["1.2.3", "1.2.3-rc.1", "1.2.3+b", "1.2.3-rc.1+b"]:
            self.assertEqual(str(Version.parse(text)), text)

    def test_compare_other_type(self) -> None:
        self.assertNotEqual(Version.parse("1.0.0"), "1.0.0")
        with self.assertRaises(TypeError):
            _ = Version.parse("1.0.0") < "1.0.1"  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
