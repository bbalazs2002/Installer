import unittest
from typing import Any

from installer.manifest import Manifest, parse_manifest
from installer.plan import (
    DOWNGRADE_PHRASE,
    BackupRef,
    ConfirmKind,
    PlanKind,
    PlanRequest,
    StepId,
    build_plan,
    format_plan,
)
from installer.state import InstallInfo, InstallRecord, TargetState
from installer.versions import Version

REPO = "o/investment"
TARGET = "/var/www/investment"


def manifest(*, database: bool = True, **extra: Any) -> Manifest:
    data: dict[str, Any] = {
        "schemaVersion": 1,
        "name": "investment",
        "displayName": "Investment",
        "image": {"name": "investment"},
        "compose": {},
        "port": {"default": 2570},
        "health": {"path": "/healthz", "timeoutSeconds": 90},
    }
    if database:
        data["database"] = {
            "required": True,
            "urlEnv": "DATABASE_URL",
            "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
        }
    data.update(extra)
    return parse_manifest(data)


def installed(version: str, migrations: tuple[str, ...] = ("m1", "m2")) -> TargetState:
    info = InstallInfo(
        name="investment",
        repo=REPO,
        version=version,
        installed_at="2026-11-18T10:00:00Z",
        migrations=migrations,
    )
    return TargetState(exists=True, empty=False, install_info=info)


def request(
    version: str,
    state: TargetState | None = None,
    *,
    mf: Manifest | None = None,
    migrations: tuple[str, ...] | None = ("m1", "m2"),
    **kwargs: Any,
) -> PlanRequest:
    return PlanRequest(
        repo=kwargs.pop("repo", REPO),
        version=Version.parse(version),
        target=TARGET,
        manifest=mf or manifest(),
        target_state=state or TargetState(exists=False, empty=True),
        requested_migrations=migrations,
        **kwargs,
    )


def step_ids(plan: Any) -> list[StepId]:
    return [s.id for s in plan.steps]


class NewInstallTests(unittest.TestCase):
    def test_missing_target(self) -> None:
        plan = build_plan(request("0.1.0"))
        self.assertEqual(plan.kind, PlanKind.NEW)
        self.assertTrue(plan.can_proceed)
        self.assertIsNone(plan.installed_version)
        self.assertEqual(plan.warnings, ())
        assert plan.confirmation is not None
        self.assertEqual(plan.confirmation.kind, ConfirmKind.CONFIRM)

    def test_empty_existing_target(self) -> None:
        plan = build_plan(request("0.1.0", TargetState(exists=True, empty=True)))
        self.assertEqual(plan.kind, PlanKind.NEW)
        self.assertTrue(plan.can_proceed)

    def test_non_empty_foreign_target_blocks(self) -> None:
        plan = build_plan(request("0.1.0", TargetState(exists=True, empty=False)))
        self.assertEqual(plan.kind, PlanKind.NEW)
        self.assertFalse(plan.can_proceed)
        self.assertIn("nem üres", plan.blockers[0])

    def test_steps_with_and_without_database(self) -> None:
        with_db = step_ids(build_plan(request("0.1.0")))
        self.assertEqual(
            with_db,
            [
                StepId.LOCK, StepId.DOWNLOAD, StepId.ENV, StepId.DB_CREATE, StepId.LOAD_IMAGE,
                StepId.ACTIVATE, StepId.START, StepId.HEALTH, StepId.FINALIZE,
            ],
        )  # fmt: skip
        no_db = step_ids(build_plan(request("0.1.0", mf=manifest(database=False))))
        self.assertNotIn(StepId.DB_CREATE, no_db)
        self.assertNotIn(StepId.DB_BACKUP, no_db)

    def test_prerelease_and_manifest_warnings_are_carried(self) -> None:
        mf = manifest(futureKey=1)
        plan = build_plan(request("0.2.0-rc.1", mf=mf))
        self.assertTrue(any("előzetes" in w for w in plan.warnings))
        self.assertTrue(any("futureKey" in w for w in plan.warnings))

    def test_registry_name_collision_elsewhere_blocks(self) -> None:
        other = InstallRecord("investment", REPO, "/srv/other", "0.1.0", "2026-01-01T00:00:00Z")
        plan = build_plan(request("0.1.0", registry=[other]))
        self.assertFalse(plan.can_proceed)
        self.assertIn("/srv/other", plan.blockers[0])

    def test_registry_entry_for_same_place_is_fine(self) -> None:
        same = InstallRecord("investment", REPO, TARGET, "0.1.0", "2026-01-01T00:00:00Z")
        self.assertTrue(build_plan(request("0.1.0", registry=[same])).can_proceed)


class UpdateTests(unittest.TestCase):
    def test_newer_version_is_update(self) -> None:
        plan = build_plan(request("0.2.0", installed("0.1.0")))
        self.assertEqual(plan.kind, PlanKind.UPDATE)
        self.assertTrue(plan.can_proceed)
        self.assertEqual(plan.installed_version, "0.1.0")
        self.assertIn("Ez frissítés, nem új telepítés: 0.1.0 → 0.2.0", plan.warnings)
        ids = step_ids(plan)
        self.assertIn(StepId.COMPOSE_REVIEW, ids)
        self.assertIn(StepId.DB_BACKUP, ids)
        self.assertNotIn(StepId.DB_CREATE, ids)
        self.assertLess(ids.index(StepId.DB_BACKUP), ids.index(StepId.LOAD_IMAGE))

    def test_numeric_not_lexical_comparison(self) -> None:
        self.assertEqual(build_plan(request("0.10.0", installed("0.9.0"))).kind, PlanKind.UPDATE)

    def test_prerelease_to_final_is_update(self) -> None:
        self.assertEqual(
            build_plan(request("1.0.0", installed("1.0.0-rc.1"))).kind, PlanKind.UPDATE
        )

    def test_no_database_means_no_backup_step(self) -> None:
        plan = build_plan(request("0.2.0", installed("0.1.0"), mf=manifest(database=False)))
        self.assertNotIn(StepId.DB_BACKUP, step_ids(plan))


class SameVersionTests(unittest.TestCase):
    def test_same_needs_explicit_confirmation(self) -> None:
        plan = build_plan(request("0.1.0", installed("0.1.0")))
        self.assertEqual(plan.kind, PlanKind.SAME)
        self.assertTrue(plan.can_proceed)
        self.assertTrue(any("már telepítve" in w for w in plan.warnings))
        assert plan.confirmation is not None
        self.assertEqual(plan.confirmation.kind, ConfirmKind.CONFIRM)
        self.assertIn("Újratelepíted", plan.confirmation.prompt)

    def test_build_metadata_does_not_make_it_different(self) -> None:
        self.assertEqual(build_plan(request("0.1.0+b2", installed("0.1.0+b1"))).kind, PlanKind.SAME)


class DowngradeTests(unittest.TestCase):
    def test_identical_migrations_allowed_with_typed_confirmation(self) -> None:
        plan = build_plan(
            request("0.1.0", installed("0.2.0", ("m1", "m2")), migrations=("m1", "m2"))
        )
        self.assertEqual(plan.kind, PlanKind.DOWNGRADE)
        self.assertTrue(plan.can_proceed)
        self.assertEqual(plan.extra_migrations, ())
        assert plan.confirmation is not None
        self.assertEqual(plan.confirmation.kind, ConfirmKind.TYPE)
        self.assertEqual(plan.confirmation.phrase, DOWNGRADE_PHRASE)
        self.assertTrue(plan.confirmation.accepts("VISSZALÉPÉS"))
        self.assertFalse(plan.confirmation.accepts("visszalépés"))
        self.assertFalse(plan.confirmation.accepts(""))
        self.assertTrue(any(w.startswith("VISSZALÉPÉS") for w in plan.warnings))

    def test_requested_has_more_migrations_is_allowed(self) -> None:
        plan = build_plan(request("0.1.0", installed("0.2.0", ("m1",)), migrations=("m1", "m2")))
        self.assertTrue(plan.can_proceed)

    def test_installed_has_extra_migrations_blocks(self) -> None:
        plan = build_plan(
            request("0.1.0", installed("0.2.0", ("m1", "m2", "m3")), migrations=("m1",))
        )
        self.assertEqual(plan.kind, PlanKind.DOWNGRADE)
        self.assertFalse(plan.can_proceed)
        self.assertEqual(plan.extra_migrations, ("m2", "m3"))
        self.assertIsNone(plan.restore_backup)
        self.assertTrue(any("Nincs" in w and "mentés" in w for w in plan.warnings))

    def test_blocked_downgrade_offers_matching_backup(self) -> None:
        backups = [
            BackupRef("0.1.0", "/b/old.dump", "2026-10-01T00:00:00Z"),
            BackupRef("0.1.0", "/b/new.dump", "2026-11-01T00:00:00Z"),
            BackupRef("0.0.9", "/b/other.dump", "2026-11-10T00:00:00Z"),
        ]
        plan = build_plan(
            request("0.1.0", installed("0.2.0", ("m1", "m2")), migrations=("m1",), backups=backups)
        )
        self.assertFalse(plan.can_proceed)
        assert plan.restore_backup is not None
        self.assertEqual(plan.restore_backup.path, "/b/new.dump")
        self.assertTrue(any("elvesznek" in w for w in plan.warnings))

    def test_many_extra_migrations_are_summarised(self) -> None:
        many = tuple(f"m{i}" for i in range(9))
        plan = build_plan(request("0.1.0", installed("0.2.0", many), migrations=()))
        self.assertIn("+4 további", plan.blockers[0])

    def test_missing_requested_migration_list_blocks_when_database(self) -> None:
        plan = build_plan(request("0.1.0", installed("0.2.0"), migrations=None))
        self.assertFalse(plan.can_proceed)

    def test_without_database_only_typed_confirmation(self) -> None:
        plan = build_plan(
            request(
                "0.1.0",
                installed("0.2.0", ("m1", "m2", "m3")),
                mf=manifest(database=False),
                migrations=None,
            )
        )
        self.assertEqual(plan.kind, PlanKind.DOWNGRADE)
        self.assertTrue(plan.can_proceed)
        assert plan.confirmation is not None
        self.assertEqual(plan.confirmation.kind, ConfirmKind.TYPE)

    def test_prerelease_below_installed_final_is_downgrade(self) -> None:
        self.assertEqual(
            build_plan(request("1.0.0-rc.1", installed("1.0.0"))).kind, PlanKind.DOWNGRADE
        )


class BlockingStateTests(unittest.TestCase):
    def test_other_repo(self) -> None:
        plan = build_plan(request("0.1.0", installed("0.1.0"), repo="someone/else"))
        self.assertEqual(plan.kind, PlanKind.OTHER_REPO)
        self.assertFalse(plan.can_proceed)
        self.assertEqual(plan.steps, ())
        self.assertIsNone(plan.confirmation)

    def test_other_app_name_in_same_repo(self) -> None:
        info = InstallInfo("different", REPO, "0.1.0", "2026-11-18T10:00:00Z")
        state = TargetState(exists=True, empty=False, install_info=info)
        self.assertEqual(build_plan(request("0.1.0", state)).kind, PlanKind.OTHER_REPO)

    def test_unreadable_install_json_blocks(self) -> None:
        state = TargetState(exists=True, empty=False, unreadable_reason="nem érvényes JSON")
        plan = build_plan(request("0.1.0", state))
        self.assertEqual(plan.kind, PlanKind.BLOCKED)
        self.assertFalse(plan.can_proceed)
        self.assertIn("nem érvényes JSON", plan.blockers[0])

    def test_unparsable_installed_version_blocks(self) -> None:
        plan = build_plan(request("0.1.0", installed("garbage")))
        self.assertEqual(plan.kind, PlanKind.BLOCKED)
        self.assertFalse(plan.can_proceed)


class FormatTests(unittest.TestCase):
    def test_format_contains_kind_warnings_and_steps(self) -> None:
        text = format_plan(build_plan(request("0.2.0", installed("0.1.0"))))
        self.assertIn("Frissítés", text)
        self.assertIn("0.1.0 → 0.2.0", text)
        self.assertIn("Lépések:", text)
        self.assertIn("/healthz", text)

    def test_format_blocked_hides_steps(self) -> None:
        text = format_plan(build_plan(request("0.1.0", TargetState(exists=True, empty=False))))
        self.assertIn("Akadályok:", text)
        self.assertNotIn("Lépések:", text)


if __name__ == "__main__":
    unittest.main()
