import copy
import json
import unittest
from typing import Any

from installer.manifest import ManifestError, load_manifest, parse_manifest

# The Investment example from the specification (chapter 2).
EXAMPLE: dict[str, Any] = {
    "schemaVersion": 1,
    "name": "investment",
    "displayName": "Investment",
    "image": {"name": "investment"},
    "compose": {"file": "docker-compose.deploy.yml", "projectName": "investment"},
    "port": {"default": 2570, "env": "PORT"},
    "health": {"path": "/healthz", "timeoutSeconds": 120, "versionField": "version"},
    "database": {
        "required": True,
        "engine": "postgres",
        "description": "Postgres kell.",
        "urlEnv": "DATABASE_URL",
        "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
        "defaultDatabase": "investment",
        "defaultUser": "investment",
    },
    "migrations": {"listAsset": "migrations.txt"},
    "env": [
        {"name": "NODE_ENV", "fixed": "production"},
        {
            "name": "BASE_PATH",
            "default": "/investment/",
            "validate": "^/([a-z0-9._~-]+/)*$",
            "group": "basic",
        },
        {
            "name": "APIKEY_MASTER_SECRET",
            "description": "titok",
            "rotationWarning": "Csere után a kulcsok nem láthatók.",
            "secret": True,
            "required": True,
            "generate": {"bytes": 48},
            "minLength": 32,
            "group": "basic",
        },
        {"name": "INTRADAY_COLLECT_ENABLED", "default": "false", "group": "advanced"},
    ],
    "proxy": {"note": "Apache", "snippet": 'ProxyPass "/x/" "http://127.0.0.1:{port}/"'},
    "postInstall": ["Hozz létre felhasználót"],
    "keep": 3,
}


def example() -> dict[str, Any]:
    return copy.deepcopy(EXAMPLE)


class ValidManifestTests(unittest.TestCase):
    def test_full_example(self) -> None:
        m = parse_manifest(example())
        self.assertEqual(m.name, "investment")
        self.assertEqual(m.project_name, "investment")
        self.assertEqual(m.port_default, 2570)
        self.assertEqual(m.health_timeout_seconds, 120)
        self.assertEqual(m.keep, 3)
        self.assertEqual(m.warnings, ())
        assert m.database is not None
        self.assertEqual(m.database.url_env, "DATABASE_URL")
        self.assertEqual(m.database.default_user, "investment")
        self.assertEqual(m.migrations_asset, "migrations.txt")
        secret = m.env_by_name("APIKEY_MASTER_SECRET")
        assert secret is not None
        self.assertEqual(secret.generate_bytes, 48)
        self.assertEqual(secret.min_length, 32)
        self.assertEqual(secret.rotation_warning, "Csere után a kulcsok nem láthatók.")
        advanced = m.env_by_name("INTRADAY_COLLECT_ENABLED")
        assert advanced is not None
        self.assertEqual(advanced.group, "advanced")
        self.assertIsNone(m.env_by_name("NOPE"))

    def test_minimal_manifest_uses_defaults(self) -> None:
        m = parse_manifest(
            {
                "schemaVersion": 1,
                "name": "app",
                "image": {"name": "app"},
                "compose": {},
                "port": {"default": 8080},
                "health": {"path": "/h"},
            }
        )
        self.assertEqual(m.display_name, "app")
        self.assertEqual(m.compose_file, "docker-compose.deploy.yml")
        self.assertEqual(m.project_name, "app")
        self.assertEqual(m.port_env, "PORT")
        self.assertEqual(m.health_timeout_seconds, 120)
        self.assertEqual(m.health_version_field, "version")
        self.assertEqual(m.keep, 3)
        self.assertIsNone(m.database)
        self.assertIsNone(m.migrations_asset)
        self.assertEqual(m.env, ())

    def test_database_not_required_means_no_database(self) -> None:
        data = example()
        data["database"] = {"required": False}
        self.assertIsNone(parse_manifest(data).database)

    def test_proxy_snippet_port_substitution(self) -> None:
        m = parse_manifest(example())
        self.assertIn("127.0.0.1:2571/", m.proxy_snippet_for(2571))
        self.assertNotIn("{port}", m.proxy_snippet_for(2571))

    def test_load_from_bytes(self) -> None:
        m = load_manifest(json.dumps(example()).encode("utf-8"))
        self.assertEqual(m.name, "investment")


class WarningTests(unittest.TestCase):
    def test_unknown_keys_warn_but_pass(self) -> None:
        data = example()
        data["futureThing"] = 1
        data["health"]["extra"] = True
        data["env"][1]["newField"] = "x"
        m = parse_manifest(data)
        self.assertEqual(len(m.warnings), 3)
        self.assertTrue(any("futureThing" in w for w in m.warnings))
        self.assertTrue(any("health.extra" in w for w in m.warnings))

    def test_rotation_warning_on_non_secret_is_dropped(self) -> None:
        data = example()
        data["env"][1]["rotationWarning"] = "nope"
        m = parse_manifest(data)
        item = m.env_by_name("BASE_PATH")
        assert item is not None
        self.assertIsNone(item.rotation_warning)
        self.assertTrue(any("rotationWarning" in w for w in m.warnings))


class InvalidManifestTests(unittest.TestCase):
    def assert_invalid(self, data: Any, fragment: str = "") -> None:
        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_missing_required_sections(self) -> None:
        for key in ["schemaVersion", "name", "image", "compose", "port", "health"]:
            data = example()
            del data[key]
            with self.subTest(key=key):
                self.assert_invalid(data, key)

    def test_missing_nested_required(self) -> None:
        data = example()
        del data["port"]["default"]
        self.assert_invalid(data, "port.default")
        data = example()
        del data["health"]["path"]
        self.assert_invalid(data, "health.path")
        data = example()
        del data["image"]["name"]
        self.assert_invalid(data, "image.name")

    def test_newer_schema_version(self) -> None:
        data = example()
        data["schemaVersion"] = 2
        self.assert_invalid(data, "Frissítsd a telepítőt")

    def test_not_an_object(self) -> None:
        self.assert_invalid([], "deploy.json")
        with self.assertRaises(ManifestError):
            load_manifest("{not json")

    def test_bad_names(self) -> None:
        for bad in ["Investment", "1app", "a_b", "", "a" * 42, "../x"]:
            data = example()
            data["name"] = bad
            with self.subTest(name=bad), self.assertRaises(ManifestError):
                parse_manifest(data)

    def test_compose_file_must_be_plain_name(self) -> None:
        for bad in ["../x.yml", "a/b.yml", "/etc/passwd", ".hidden", ""]:
            data = example()
            data["compose"]["file"] = bad
            with self.subTest(file=bad), self.assertRaises(ManifestError):
                parse_manifest(data)

    def test_bad_port(self) -> None:
        for bad in [0, 70000, "2570", True, -1, 1.5]:
            data = example()
            data["port"]["default"] = bad
            with self.subTest(port=bad), self.assertRaises(ManifestError):
                parse_manifest(data)

    def test_health_path_must_start_with_slash(self) -> None:
        data = example()
        data["health"]["path"] = "healthz"
        self.assert_invalid(data, "health.path")

    def test_database_rules(self) -> None:
        data = example()
        data["database"]["engine"] = "mysql"
        self.assert_invalid(data, "mysql")
        data = example()
        data["database"]["urlTemplate"] = "postgresql://{user}:{pass}@{host}/{database}"
        self.assert_invalid(data, "{pass}")
        data = example()
        del data["database"]["urlEnv"]
        self.assert_invalid(data, "database.urlEnv")
        data = example()
        data["database"]["defaultUser"] = "Bad-Name"
        self.assert_invalid(data, "defaultUser")

    def test_url_env_must_not_be_listed_in_env(self) -> None:
        data = example()
        data["env"].append({"name": "DATABASE_URL", "default": "x"})
        self.assert_invalid(data, "DATABASE_URL")

    def test_port_env_must_not_be_listed_in_env(self) -> None:
        data = example()
        data["env"].append({"name": "PORT", "default": "1"})
        self.assert_invalid(data, "PORT")

    def test_env_item_rules(self) -> None:
        def with_item(item: dict[str, Any]) -> dict[str, Any]:
            data = example()
            data["env"].append(item)
            return data

        self.assert_invalid(with_item({"name": "1BAD"}), "1BAD")
        self.assert_invalid(with_item({"description": "no name"}), "name")
        self.assert_invalid(with_item({"name": "X", "validate": "("}), "validate")
        self.assert_invalid(
            with_item({"name": "X", "default": "no", "validate": "^yes$"}), "validate"
        )
        self.assert_invalid(with_item({"name": "X", "default": "a", "fixed": "b"}), "fixed")
        self.assert_invalid(with_item({"name": "X", "secret": True, "fixed": "b"}), "fixed")
        self.assert_invalid(with_item({"name": "X", "generate": {"bytes": 8}}), "generate")
        self.assert_invalid(
            with_item({"name": "X", "secret": True, "generate": {"bytes": 0}}), "bytes"
        )
        self.assert_invalid(with_item({"name": "X", "group": "other"}), "group")
        self.assert_invalid(with_item({"name": "X", "required": "yes"}), "required")
        self.assert_invalid(with_item({"name": "X", "minLength": -1}), "minLength")
        self.assert_invalid(with_item({"name": "NODE_ENV"}), "kétszer")

    def test_keep_rules(self) -> None:
        for bad in [0, -1, "3", True]:
            data = example()
            data["keep"] = bad
            with self.subTest(keep=bad), self.assertRaises(ManifestError):
                parse_manifest(data)


if __name__ == "__main__":
    unittest.main()
