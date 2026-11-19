"""Data volumes: ``deploy.json`` keys, the compose model check, the compose review and the
Docker helper call. Pure logic and the command line; the installer flows are in
test_volume_flow."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from installer.composediff import review_compose, review_new_compose
from installer.docker import DockerError, HelperMount
from installer.docker_cli import SubprocessDocker
from installer.manifest import ManifestError, VolumeSpec, load_manifest, parse_manifest
from installer.validators import is_container_path, is_relative_data_path, is_volume_name
from installer.volumes import (
    ComposeConfigError,
    check_volumes,
    describe,
    parse_compose_config,
)

from .fake_release import MANIFEST
from .test_docker_cli_health import Runner

DATA = "/srv/app/shared/data"
UPLOADS = VolumeSpec("uploads", "/app/uploads", "Uploaded files")
MEDIA = VolumeSpec("media", "/app/media", kind="bind", host_path="media")


def config(mounts: list[dict[str, object]], top: dict[str, object] | None = None) -> dict:  # type: ignore[type-arg]
    return {"name": "app", "services": {"app": {"volumes": mounts}}, "volumes": top or {}}


def named(source: str = "uploads", target: str = "/app/uploads") -> dict[str, object]:
    return {"type": "volume", "source": source, "target": target}


def bind(source: str, target: str = "/app/media") -> dict[str, object]:
    return {"type": "bind", "source": source, "target": target}


def check(raw: dict, declared: tuple[VolumeSpec, ...]):  # type: ignore[no-untyped-def, type-arg]
    return check_volumes(parse_compose_config(raw), declared, project="app", data_dir=DATA)


class ValidatorTests(unittest.TestCase):
    def test_volume_names(self) -> None:
        for good in ("data", "uploads", "a1_b2"):
            self.assertTrue(is_volume_name(good), good)
        for bad in ("", "1data", "Data", "my-data", "a.b", "x" * 33):
            self.assertFalse(is_volume_name(bad), bad)

    def test_container_paths(self) -> None:
        self.assertTrue(is_container_path("/app/data"))
        self.assertTrue(is_container_path("/var/lib/x/"))
        for bad in ("app/data", "/", "/a/../b", "/a/./b", "/a:b", "/a'b", '/a"b', "/a\nb", "/a\\b"):
            self.assertFalse(is_container_path(bad), repr(bad))

    def test_relative_data_paths(self) -> None:
        self.assertTrue(is_relative_data_path("media"))
        self.assertTrue(is_relative_data_path("a/b.c/d-e"))
        for bad in ("", "/abs", "../x", "a/../b", "a//b", ".hidden", "a b", "a/b/c/d/e"):
            self.assertFalse(is_relative_data_path(bad), repr(bad))


class ManifestVolumeTests(unittest.TestCase):
    def manifest(self, volumes: object) -> dict:  # type: ignore[type-arg]
        data = copy.deepcopy(MANIFEST)
        data["volumes"] = volumes
        return data

    def test_no_volumes_by_default(self) -> None:
        self.assertEqual(parse_manifest(copy.deepcopy(MANIFEST)).volumes, ())

    def test_named_and_bind_volumes(self) -> None:
        parsed = parse_manifest(
            self.manifest(
                [
                    {"name": "uploads", "mountPath": "/app/uploads", "description": "Files"},
                    {
                        "name": "media",
                        "kind": "bind",
                        "hostPath": "media/pics",
                        "mountPath": "/app/media",
                        "backup": False,
                    },
                ]
            )
        )
        uploads, media = parsed.volumes
        self.assertEqual((uploads.kind, uploads.backup, uploads.host_path), ("named", True, None))
        self.assertEqual((media.kind, media.backup, media.host_path), ("bind", False, "media/pics"))
        self.assertTrue(media.is_bind and not uploads.is_bind)
        self.assertEqual(parsed.warnings, ())  # the key is known

    def test_invalid_volumes_are_rejected(self) -> None:
        ok = {"name": "data", "mountPath": "/app/data"}
        cases = {
            "not a list": {"name": "data"},
            "bad name": [{**ok, "name": "Bad-Name"}],
            "no mountPath": [{"name": "data"}],
            "relative mountPath": [{**ok, "mountPath": "app/data"}],
            "unknown kind": [{**ok, "kind": "nfs"}],
            "bind without hostPath": [{**ok, "kind": "bind"}],
            "hostPath on a named volume": [{**ok, "hostPath": "x"}],
            "hostPath escapes": [{**ok, "kind": "bind", "hostPath": "../etc"}],
            "absolute hostPath": [{**ok, "kind": "bind", "hostPath": "/etc"}],
            "backup is not a boolean": [{**ok, "backup": "yes"}],
            "duplicate name": [ok, {**ok, "mountPath": "/other"}],
            "duplicate mountPath": [ok, {"name": "more", "mountPath": "/app/data/"}],
            "duplicate hostPath": [
                {**ok, "kind": "bind", "hostPath": "x"},
                {"name": "more", "mountPath": "/m", "kind": "bind", "hostPath": "x"},
            ],
        }
        for label, volumes in cases.items():
            with self.subTest(label), self.assertRaises(ManifestError):
                parse_manifest(self.manifest(volumes))

    def test_unknown_key_in_a_volume_is_only_a_warning(self) -> None:
        parsed = parse_manifest(
            self.manifest([{"name": "data", "mountPath": "/app/data", "size": "10G"}])
        )
        self.assertTrue(any("volumes[0].size" in w for w in parsed.warnings))

    def test_base_path_placeholder_needs_the_env_variable(self) -> None:
        data = copy.deepcopy(MANIFEST)
        data["proxy"] = {"snippet": 'ProxyPass "{basePath}" "http://127.0.0.1:{port}/"'}
        manifest = parse_manifest(data)  # BASE_PATH is declared in the sample
        self.assertEqual(
            manifest.proxy_snippet_for(2570, "/games/"),
            'ProxyPass "/games/" "http://127.0.0.1:2570/"',
        )
        data["env"] = [e for e in data["env"] if e["name"] != "BASE_PATH"]
        with self.assertRaises(ManifestError):
            parse_manifest(data)

    def test_snippet_without_a_base_path_keeps_the_placeholder_literal(self) -> None:
        manifest = parse_manifest(copy.deepcopy(MANIFEST))
        self.assertEqual(
            manifest.proxy_snippet_for(1), 'ProxyPass "/investment/" "http://127.0.0.1:1/"'
        )
        data = copy.deepcopy(MANIFEST)
        data["proxy"] = {"snippet": "{basePath} {port}"}
        self.assertEqual(parse_manifest(data).proxy_snippet_for(7), "{basePath} 7")

    def test_loads_from_json_text(self) -> None:
        data = self.manifest([{"name": "data", "mountPath": "/app/data"}])
        self.assertEqual(load_manifest(json.dumps(data)).volumes[0].name, "data")


class ComposeModelTests(unittest.TestCase):
    def test_parse(self) -> None:
        model = parse_compose_config(
            {
                "services": {
                    "app": {"volumes": [named(), bind(f"{DATA}/media")]},
                    "worker": {"volumes": [named()], "image": "x"},
                    "plain": {},
                },
                "volumes": {
                    "uploads": {"name": "app_uploads"},
                    "ext": {"name": "ext", "external": True},
                },
            }
        )
        self.assertEqual(len(model.mounts), 3)
        self.assertEqual(model.volume_names["uploads"], "app_uploads")
        self.assertEqual(model.external, frozenset({"ext"}))

    def test_no_volumes_at_all(self) -> None:
        model = parse_compose_config({"services": {"app": {"image": "x"}}})
        self.assertEqual((model.mounts, model.volume_names), ((), {}))

    def test_wrong_shapes(self) -> None:
        for bad in (
            [],
            {"services": "x"},
            {"services": {"a": {"volumes": ["x"]}}},
            {"volumes": "x"},
        ):
            with self.subTest(bad), self.assertRaises(ComposeConfigError):
                parse_compose_config(bad)


class CheckVolumesTests(unittest.TestCase):
    top = {"uploads": {"name": "app_uploads"}}

    def test_nothing_declared_nothing_mounted(self) -> None:
        result = check(config([]), ())
        self.assertTrue(result.ok)
        self.assertEqual((result.resolved, result.anonymous), ((), ()))

    def test_named_volume_resolves_to_the_project_volume(self) -> None:
        result = check(config([named()], self.top), (UPLOADS,))
        self.assertTrue(result.ok, result.problems)
        (resolved,) = result.resolved
        self.assertEqual(
            (resolved.name, resolved.source, resolved.is_bind), ("uploads", "app_uploads", False)
        )

    def test_a_missing_top_level_entry_uses_the_default_name(self) -> None:
        self.assertEqual(check(config([named()]), (UPLOADS,)).resolved[0].source, "app_uploads")

    def test_bind_below_the_data_directory(self) -> None:
        result = check(config([bind(f"{DATA}/media")]), (MEDIA,))
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.resolved[0].source, f"{DATA}/media")
        self.assertTrue(result.resolved[0].is_bind)

    def test_windows_style_data_dir_is_normalised(self) -> None:
        result = check_volumes(
            parse_compose_config(config([bind("C:/srv/shared/data/media")])),
            (MEDIA,),
            project="app",
            data_dir="C:\\srv\\shared\\data",
        )
        self.assertTrue(result.ok, result.problems)

    def test_both_kinds_together_keep_the_declared_order(self) -> None:
        result = check(config([bind(f"{DATA}/media"), named()], self.top), (UPLOADS, MEDIA))
        self.assertEqual([v.name for v in result.resolved], ["uploads", "media"])

    def test_tmpfs_is_ignored(self) -> None:
        result = check(config([{"type": "tmpfs", "target": "/tmp"}]), ())
        self.assertTrue(result.ok)

    def test_anonymous_volumes_are_reported_not_blocked(self) -> None:
        result = check(config([{"type": "volume", "target": "/cache"}]), ())
        self.assertTrue(result.ok)
        self.assertEqual([m.target for m in result.anonymous], ["/cache"])

    def blocked(self, raw: dict, declared: tuple[VolumeSpec, ...], text: str) -> None:  # type: ignore[type-arg]
        result = check(raw, declared)
        self.assertFalse(result.ok)
        self.assertTrue(any(text in p for p in result.problems), result.problems)

    def test_docker_socket(self) -> None:
        self.blocked(
            config([bind("/var/run/docker.sock", "/var/run/docker.sock")]), (), "Docker socket"
        )
        self.blocked(config([bind("/run/docker.sock", "/x")]), (), "Docker socket")

    def test_bind_outside_the_data_directory(self) -> None:
        self.blocked(config([bind("/etc")]), (MEDIA,), "outside the data directory")
        self.blocked(config([bind(f"{DATA}/other")]), (MEDIA,), "does not declare")
        self.blocked(config([bind(f"{DATA}/../etc")]), (MEDIA,), "outside the data directory")

    def test_relative_bind_is_refused(self) -> None:
        self.blocked(config([bind("data/x")]), (), "relative")

    def test_external_volume(self) -> None:
        self.blocked(
            config([named()], {"uploads": {"name": "uploads", "external": True}}),
            (UPLOADS,),
            "external",
        )

    def test_renamed_volume(self) -> None:
        self.blocked(
            config([named()], {"uploads": {"name": "somebody_elses"}}), (UPLOADS,), "custom name"
        )

    def test_mounted_but_not_declared(self) -> None:
        self.blocked(config([named()], self.top), (), "does not declare")
        self.blocked(config([bind(f"{DATA}/media")]), (), "does not declare")

    def test_declared_but_not_mounted(self) -> None:
        self.blocked(config([]), (UPLOADS,), "does not mount it")

    def test_wrong_mount_path(self) -> None:
        self.blocked(config([named(target="/elsewhere")], self.top), (UPLOADS,), "different path")

    def test_problems_are_not_repeated(self) -> None:
        raw = {
            "services": {"a": {"volumes": [bind("/etc")]}, "b": {"volumes": [bind("/etc")]}},
        }
        self.assertEqual(len(check(raw, ()).problems), 1)

    def test_describe(self) -> None:
        self.assertEqual(
            describe(UPLOADS), "uploads (volume) -> /app/uploads, backed up - Uploaded files"
        )
        self.assertIn("NOT backed up", describe(VolumeSpec("m", "/m", backup=False)))
        self.assertIn("bind mount", describe(MEDIA))


class ComposeReviewTests(unittest.TestCase):
    BASE = "services:\n  app:\n    image: x\n"

    def test_user_with_the_installer_ids_is_not_risky(self) -> None:
        new = self.BASE + '    user: "${APP_UID}:${APP_GID}"\n'
        review = review_compose(self.BASE, new)
        self.assertEqual(review.flagged, ())
        self.assertIn("APP_UID", review.diff)

    def test_any_other_user_still_is(self) -> None:
        for line in ("    user: root\n", "    user: 0\n", "    user: ${SOMETHING}\n"):
            self.assertTrue(review_compose(self.BASE, self.BASE + line).needs_confirmation, line)

    def test_a_new_compose_flags_risky_lines_but_not_bare_headers(self) -> None:
        text = self.BASE + '    ports:\n      - "127.0.0.1:1:1"\n    volumes:\n      - data:/d\n'
        review = review_new_compose(text)
        self.assertEqual(review.flagged, ())
        self.assertNotIn("!!", review.diff)
        risky = review_new_compose(self.BASE + "    privileged: true\n")
        self.assertEqual(risky.flagged, ("privileged: true",))
        self.assertIn("!! +    privileged: true", risky.diff)

    def test_a_new_compose_still_flags_an_exposed_port(self) -> None:
        review = review_new_compose(self.BASE + '    ports:\n      - "8080:80"\n')
        self.assertEqual(review.flagged, ('- "8080:80"',))

    def test_build_is_still_detected(self) -> None:
        self.assertTrue(review_new_compose(self.BASE + "    build: .\n").build_present)


class HelperContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner()
        self.docker = SubprocessDocker(runner=self.runner)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_arguments_of_a_helper_run(self) -> None:
        self.runner.push()
        out = self.dir / "a.tar.gz"
        self.docker.run_helper(
            "img:1",
            ["sh", "-c", "tar czf - -C /data ."],
            mounts=[HelperMount("app_uploads", "/data", read_only=True)],
            stdout_to=out,
        )
        call = self.runner.calls[0]
        self.assertEqual(
            call["command"],
            [
                "docker", "run", "--rm", "-i", "--pull", "never", "--network", "none",
                "--user", "0", "-v", "app_uploads:/data:ro", "--entrypoint", "sh",
                "img:1", "-c", "tar czf - -C /data .",
            ],
        )  # fmt: skip
        self.assertIs(call["shell"], False)
        self.assertGreater(call["timeout"], 0)

    def test_a_writable_mount_and_stdin(self) -> None:
        self.runner.push()
        source = self.dir / "in.tar.gz"
        source.write_bytes(b"x")
        self.docker.run_helper(
            "img:1",
            ["sh", "-c", "true"],
            mounts=[HelperMount("/srv/d", "/data")],
            stdin_from=source,
        )
        command = self.runner.calls[0]["command"]
        self.assertIn("/srv/d:/data", command)
        self.assertNotIn("/srv/d:/data:ro", command)

    def test_dangerous_mount_sources_are_refused(self) -> None:
        for bad in ("/srv/a:b", "/srv/a,b"):
            with self.subTest(bad), self.assertRaises(DockerError):
                self.docker.run_helper("i", ["sh"], mounts=[HelperMount(bad, "/data")])
        self.assertEqual(self.runner.calls, [])

    def test_failure_is_returned_and_stderr_masked(self) -> None:
        self.docker._redactor.register("hunter2hunter2")
        self.runner.push(rc=2, err=b"boom hunter2hunter2")
        result = self.docker.run_helper("i", ["sh"], mounts=[])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"hunter2", result.stderr)

    def test_compose_config_arguments_and_parsing(self) -> None:
        self.runner.push(out=json.dumps({"services": {}}).encode())
        compose = self.dir / "r" / "docker-compose.deploy.yml"
        env = self.dir / "shared" / "compose.env"
        self.assertEqual(self.docker.compose_config("app", compose, env), {"services": {}})
        call = self.runner.calls[0]
        self.assertEqual(
            call["command"],
            ["docker", "compose", "-p", "app", "-f", str(compose), "--env-file", str(env),
             "config", "--format", "json"],
        )  # fmt: skip
        self.assertEqual(call["cwd"], compose.parent)

    def test_compose_config_errors(self) -> None:
        self.runner.push(rc=1, err=b"invalid compose file")
        with self.assertRaises(DockerError):
            self.docker.compose_config("a", self.dir / "c.yml", self.dir / "e")
        self.runner.push(out=b"not json")
        with self.assertRaises(DockerError):
            self.docker.compose_config("a", self.dir / "c.yml", self.dir / "e")
        self.runner.push(out=b"[]")
        with self.assertRaises(DockerError):
            self.docker.compose_config("a", self.dir / "c.yml", self.dir / "e")

    def test_volume_list(self) -> None:
        self.runner.push(out=b"app_uploads\napp_data\n")
        self.assertEqual(self.docker.volume_list("app"), ["app_uploads", "app_data"])
        command = self.runner.calls[0]["command"]
        self.assertIn("label=com.docker.compose.project=app", command)


if __name__ == "__main__":
    unittest.main()
