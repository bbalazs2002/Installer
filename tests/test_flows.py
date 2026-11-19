import os
import shutil
import unittest

from installer.app import run_action, run_menu
from installer.backup import group_sets, list_all_backups
from installer.errors import InstallerError
from installer.flows_data import backup_menu, set_key
from installer.flows_env import env_screen, rotate_screen, vault_menu
from installer.flows_install import OTHER_REPO, TOGGLE_PRERELEASES, install_flow
from installer.flows_misc import (
    logs_screen,
    pending_updates,
    rollback_screen,
    settings_menu,
    status_screen,
    updates_screen,
)
from installer.state import KnownRepo, load_install_info, load_installs, load_repos, save_repos
from installer.ui.base import UiBack
from installer.vault import Vault

from .appsupport import REPO, AppTestCase, D


class InstallFlowTests(AppTestCase):
    def answers_to_release(self, tag: str = "v0.1.0") -> list[object]:
        return [OTHER_REPO, REPO, tag, "install"]

    def test_new_installation_end_to_end(self) -> None:
        self.publish("0.1.0")
        self.ui.push(*self.answers_to_release(), str(self.target), D, "run", True, True)
        install_flow(self.ctx)

        self.assertEqual(self.ui.remaining, 0)
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual((info.version, info.port), ("0.1.0", 2570))
        self.assertEqual([r.name for r in load_installs(self.paths.installs_file)], ["investment"])
        self.assertEqual(
            load_repos(self.paths.repos_file), [KnownRepo(REPO, str(self.target.resolve()))]
        )
        summary = self.ui.messages()[-1]
        self.assertIn("127.0.0.1:2570", summary)
        self.assertIn("ProxyPass", summary)
        self.assertIn("Create a user", summary)
        self.assertEqual(list(self.paths.tmp_dir.iterdir()), [])  # temp files cleaned up

    def test_prereleases_are_hidden_until_toggled(self) -> None:
        self.publish("0.1.0")
        self.publish("0.2.0-rc.1", prerelease=True)
        self.ui.push(OTHER_REPO, REPO, TOGGLE_PRERELEASES, "v0.2.0-rc.1", "install")
        self.ui.push(str(self.target), D, "plan")  # stop at "plan only"
        install_flow(self.ctx)
        menus = [c for c in self.ui.calls if c.kind == "menu" and c.title.endswith("releases")]
        self.assertEqual(menus[0].items, (TOGGLE_PRERELEASES, "v0.1.0"))
        self.assertEqual(menus[1].items, (TOGGLE_PRERELEASES, "v0.2.0-rc.1", "v0.1.0"))
        self.assertIsNone(load_install_info(self.target))

    def test_changelog_can_be_read_before_choosing(self) -> None:
        self.publish("0.1.0")
        self.ui.push(OTHER_REPO, REPO, "v0.1.0", "notes", "install", str(self.target), D, "plan")
        install_flow(self.ctx)
        self.assertIn("No release notes", self.messages())

    def test_plan_only_changes_nothing(self) -> None:
        self.publish("0.1.0")
        self.ui.push(*self.answers_to_release(), str(self.target), D, "plan")
        install_flow(self.ctx)
        self.assertEqual(self.docker.names().count("compose_up"), 0)
        self.assertIsNone(load_install_info(self.target))
        self.assertIn("Steps:", self.messages())

    def test_invalid_target_is_explained_and_asked_again(self) -> None:
        self.publish("0.1.0")
        missing = self.root / "does-not-exist"
        self.ui.push(*self.answers_to_release(), "relative/dir", str(missing), str(self.target))
        self.ui.push(D, "plan")
        install_flow(self.ctx)
        text = self.messages()
        self.assertIn("absolute path", text)
        self.assertIn("sudo mkdir -p", text)

    def test_a_blocked_plan_stops_before_asking_for_confirmation(self) -> None:
        (self.target / "stranger.txt").write_text("x", encoding="utf-8")
        self.publish("0.1.0")
        self.ui.push(*self.answers_to_release(), str(self.target), D)
        install_flow(self.ctx)
        self.assertEqual(self.ui.remaining, 0)
        self.assertIn("not empty", self.messages())

    def test_preflight_problems_stop_the_flow(self) -> None:
        self.publish("0.1.0")
        self.ctx.port_in_use = lambda port: True
        self.ui.push(*self.answers_to_release(), str(self.target), D)
        install_flow(self.ctx)
        self.assertIn("in use", self.messages())
        self.assertEqual(self.docker.names().count("compose_up"), 0)

    def test_declining_the_final_confirmation_aborts(self) -> None:
        from installer.errors import UserAbort

        self.publish("0.1.0")
        self.ui.push(*self.answers_to_release(), str(self.target), D, "run", False)
        with self.assertRaises(UserAbort):
            install_flow(self.ctx)
        self.assertIsNone(load_install_info(self.target))

    def test_known_repo_is_offered_in_the_list_and_not_asked_twice(self) -> None:
        save_repos(self.paths.repos_file, [KnownRepo(REPO, str(self.target))])
        self.publish("0.1.0")
        self.ui.push(REPO, "v0.1.0", "install", str(self.target), D, "run", True)
        install_flow(self.ctx)
        self.assertEqual(self.ui.remaining, 0)  # no "add to known repos?" question
        first = next(c for c in self.ui.calls if c.kind == "menu")
        self.assertEqual(first.items, (REPO, OTHER_REPO))

    def test_missing_token_is_reported_through_the_menu_runner(self) -> None:
        self.tokens.delete()
        run_action(self.ctx, lambda c: install_flow(c))
        self.assertIn("No GitHub token", self.messages())


class UpdateFlowTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_install("0.1.0")
        self.ui.calls.clear()

    def test_pending_updates_count(self) -> None:
        self.assertEqual(pending_updates(self.ctx), 0)
        self.publish("0.2.0")
        self.assertEqual(pending_updates(self.ctx), 1)

    def test_pending_updates_is_silent_without_a_token(self) -> None:
        self.publish("0.2.0")
        self.tokens.delete()
        self.assertEqual(pending_updates(self.ctx), 0)

    def test_everything_current(self) -> None:
        updates_screen(self.ctx)
        self.assertIn("up to date", self.messages())

    def test_choosing_an_update_runs_the_install_flow_prefilled(self) -> None:
        self.publish("0.2.0")
        self.ui.push("investment", str(self.target), D, "run", True, False)
        updates_screen(self.ctx)
        menu = next(c for c in self.ui.calls if c.kind == "menu")
        self.assertEqual(menu.items, ("investment",))
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual((info.version, info.previous_versions), ("0.2.0", ("0.1.0",)))
        # repo and release were not asked again
        self.assertNotIn("Which repository?", [c.title for c in self.ui.calls])

    def test_no_installations(self) -> None:
        shutil.rmtree(self.state_dir)
        self.state_dir.mkdir()
        updates_screen(self.ctx)
        self.assertIn("There are no installed applications yet", self.messages())

    def test_github_errors_are_shown_per_application(self) -> None:
        def broken(repo: str) -> None:
            raise InstallerError("GitHub is unavailable")

        self.github.latest_stable = broken  # type: ignore[method-assign]
        updates_screen(self.ctx)
        self.assertIn("error: GitHub is unavailable", self.messages())


class MenuTests(AppTestCase):
    def test_main_menu_routes_and_quits(self) -> None:
        self.paths.logs_dir.mkdir(parents=True)
        (self.paths.logs_dir / "2026-11-19.log").write_text("hello\n", encoding="utf-8")
        # "logs" opens the file list (choose one, then Esc), "settings" opens its menu (Esc)
        self.ui.push("logs", "2026-11-19.log", UiBack, "settings", UiBack, "quit")
        self.assertEqual(run_menu(self.ctx), 0)
        self.assertEqual(self.ui.remaining, 0)
        self.assertIn("hello", self.messages())

    def test_update_count_is_shown_in_the_menu(self) -> None:
        seen: list[str] = []
        original = self.ui.menu

        def spy(title, items, **kwargs):  # type: ignore[no-untyped-def]
            seen.extend(i.label for i in items)
            return original(title, items, **kwargs)

        self.ui.menu = spy  # type: ignore[method-assign]
        self.ui.push("quit")
        run_menu(self.ctx, updates_hint=lambda: 2)
        self.assertTrue(any("(2 updates available)" in label for label in seen))

    def test_escape_in_the_main_menu_exits(self) -> None:
        self.ui.push(UiBack)
        self.assertEqual(run_menu(self.ctx), 0)

    def test_expected_errors_become_messages_not_crashes(self) -> None:
        def boom(ctx: object) -> None:
            raise InstallerError("something went wrong")

        run_action(self.ctx, boom)
        self.assertIn("something went wrong", self.messages())
        self.assertTrue(any("something went wrong" in r for r in self.log.records))

    def test_escape_inside_an_action_is_just_going_back(self) -> None:
        def back(ctx: object) -> None:
            raise UiBack

        run_action(self.ctx, back)
        self.assertEqual(self.ui.messages(), [])


class EnvFlowTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_install("0.1.0")
        self.ui.calls.clear()

    def test_plain_value_can_be_edited_and_the_app_restarted(self) -> None:
        self.ui.push("BASE_PATH", "/new/", True, UiBack)
        self.call(env_screen)
        self.assertEqual(self.env()["BASE_PATH"], "/new/")
        self.assertEqual(self.docker.up_log[-1][3], "0.1.0")
        self.assertIn("restarted", self.messages())

    def test_declining_the_restart(self) -> None:
        before = self.docker.names().count("compose_up")
        self.ui.push("BASE_PATH", "/new/", False, UiBack)
        self.call(env_screen)
        self.assertEqual(self.docker.names().count("compose_up"), before)

    def test_secret_rows_are_masked_in_the_list(self) -> None:
        seen: list[str] = []
        original = self.ui.menu

        def spy(title, items, **kwargs):  # type: ignore[no-untyped-def]
            seen.extend(i.label for i in items)
            return original(title, items, **kwargs)

        self.ui.menu = spy  # type: ignore[method-assign]
        secret = self.env()["APIKEY_MASTER_SECRET"]
        self.ui.push(UiBack)
        self.call(env_screen)
        joined = "\n".join(seen)
        self.assertNotIn(secret, joined)
        self.assertIn("APIKEY_MASTER_SECRET = ********", joined)

    def test_fixed_values_cannot_be_edited(self) -> None:
        self.ui.push("NODE_ENV", UiBack)
        self.call(env_screen)
        self.assertIn("cannot be changed here", self.messages())

    def test_rotating_a_secret_from_the_env_list(self) -> None:
        old = self.env()["APIKEY_MASTER_SECRET"]
        self.ui.push("APIKEY_MASTER_SECRET", True, "generate", False, UiBack)
        self.call(env_screen)
        self.assertNotEqual(self.env()["APIKEY_MASTER_SECRET"], old)

    def test_rotate_screen_restarts_when_asked(self) -> None:
        old = self.env()["APIKEY_MASTER_SECRET"]
        self.ui.push("APIKEY_MASTER_SECRET", True, "generate", True)
        self.call(rotate_screen)
        self.assertNotEqual(self.env()["APIKEY_MASTER_SECRET"], old)
        self.assertIn("restarted", self.messages())
        vault = self.ctx._vault
        assert vault is not None
        self.assertEqual(
            vault.get("investment", "APIKEY_MASTER_SECRET"), self.env()["APIKEY_MASTER_SECRET"]
        )

    def test_rotation_can_be_declined(self) -> None:
        from installer.errors import UserAbort

        old = self.env()["APIKEY_MASTER_SECRET"]
        self.ui.push("APIKEY_MASTER_SECRET", False)
        with self.assertRaises(UserAbort):
            self.call(rotate_screen)
        self.assertEqual(self.env()["APIKEY_MASTER_SECRET"], old)

    def test_vault_listing_never_shows_values(self) -> None:
        vault = self.ctx._vault
        assert vault is not None
        vault.set("investment", "APIKEY_MASTER_SECRET", "value-that-must-stay-hidden")
        self.ui.push("list", UiBack)
        self.call(vault_menu)
        text = self.messages()
        self.assertIn("APIKEY_MASTER_SECRET", text)
        self.assertNotIn("value-that-must-stay-hidden", text)

    @unittest.skipIf(shutil.which("openssl") is None, "openssl not installed")
    def test_vault_export_import_and_password_change_with_a_real_vault(self) -> None:
        real = Vault.create(self.root / "vault.enc", "first master password")
        real.set("investment", "DB_PASSWORD", "stored-db-secret-1")
        real.save()
        self.ctx._vault = real
        export = self.root / "export.enc"
        self.ui.push("export", str(export), UiBack)
        self.call(vault_menu)
        self.assertEqual(export.read_bytes(), (self.root / "vault.enc").read_bytes())

        other = Vault.create(self.root / "other.enc", "other master password")
        other.set("investment", "DB_PASSWORD", "different-value-2")
        other.set("games", "TOKEN", "games-token-value")
        other.save()
        self.ui.push("import", str(self.root / "other.enc"), "other master password", False, UiBack)
        self.call(vault_menu)
        self.assertEqual(real.get("investment", "DB_PASSWORD"), "stored-db-secret-1")  # kept
        self.assertEqual(real.get("games", "TOKEN"), "games-token-value")  # added

        self.ui.push(
            "password", "short", "second master password", "second master password", UiBack
        )
        self.call(vault_menu)
        Vault.open(self.root / "vault.enc", "second master password")

    def test_without_a_vault_the_vault_menu_explains(self) -> None:
        self.ctx._vault = None
        self.ctx.ui = self.ui
        self.ui.push("list", False, UiBack)  # decline creating a vault
        self.call(vault_menu)
        self.assertIn("without a vault", self.messages())


class DatabaseFlowTests(AppTestCase):
    with_database = True

    def setUp(self) -> None:
        super().setUp()
        self.run_install("0.1.0")
        self.ui.calls.clear()
        self.backups = self.target / "backups"

    def test_manual_backup(self) -> None:
        self.ui.push("backup", UiBack)
        self.call(backup_menu)
        dumps = list(self.backups.glob("*.dump"))
        self.assertEqual(len(dumps), 1)
        self.assertIn("The backup was created", self.messages())

    def test_restore_stops_restores_and_restarts(self) -> None:
        self.db.backup(  # a dump to restore from
            load_install_info(self.target).database,
            "0.1.0",
            self.backups,  # type: ignore[union-attr]
        )
        (dump,) = self.backups.glob("*.dump")
        (backup_set,) = group_sets(list_all_backups(self.backups))
        self.docker.calls.clear()
        self.ui.push("restore", set_key(backup_set), UiBack)
        self.call(backup_menu)
        self.assertEqual(self.db.calls[-1], f"restore:{dump.name}")
        order = self.docker.names()
        self.assertLess(order.index("compose_stop"), order.index("compose_up"))
        self.assertIn("restarted", self.messages())

    def test_restore_without_backups(self) -> None:
        self.ui.push("restore", UiBack)
        self.call(backup_menu)
        self.assertIn("no backup", self.messages())

    def test_cleanup_deletes_old_backups_and_remembers_always(self) -> None:
        self.backups.mkdir(exist_ok=True)
        (self.backups / "investment-0.0.2-20261101T000000Z.dump").write_bytes(b"y")  # newest: kept
        old = self.backups / "investment-0.0.1-20240101T000000Z.dump"
        old.write_bytes(b"x")
        long_ago = 1_600_000_000
        os.utime(old, (long_ago, long_ago))
        self.ui.push("cleanup", "always", UiBack)
        self.call(backup_menu)
        self.assertFalse(old.exists())
        info = load_install_info(self.target)
        assert info is not None
        self.assertTrue(info.auto_delete_old_backups)

    def test_database_password_rotation_from_the_passwords_menu(self) -> None:
        self.ui.push("__database__", False)  # pick the database entry; no restart
        self.call(rotate_screen)
        self.assertIn("rotate", self.db.calls)

    def test_no_installation_with_a_database(self) -> None:
        shutil.rmtree(self.target)
        self.target.mkdir()
        self.call(backup_menu)
        self.assertIn("no suitable", self.messages())


class RollbackFlowTests(AppTestCase):
    def test_rollback_to_the_previous_version(self) -> None:
        self.run_install("0.1.0")
        self.run_install("0.2.0")
        self.ui.calls.clear()
        self.ui.push("0.1.0", "DOWNGRADE")
        rollback_screen(self.ctx)
        info = load_install_info(self.target)
        assert info is not None
        self.assertEqual(info.version, "0.1.0")
        self.assertIn("Version 0.1.0 is running", self.messages())

    def test_nothing_to_roll_back_to(self) -> None:
        self.run_install("0.1.0")
        rollback_screen(self.ctx)
        self.assertIn("no previous version to roll back to", self.messages())


class StatusAndLogTests(AppTestCase):
    def test_status_shows_health_token_and_recent_log_lines(self) -> None:
        self.run_install("0.1.0")
        self.paths.logs_dir.mkdir(parents=True)
        (self.paths.logs_dir / "2026-11-19.log").write_text(
            "\n".join(f"line {i}" for i in range(30)), encoding="utf-8"
        )
        self.ui.calls.clear()
        status_screen(self.ctx)
        text = self.messages()
        self.assertIn("investment  0.1.0", text)
        self.assertIn("status: ok", text)
        self.assertIn("GitHub token: valid", text)
        self.assertIn("line 29", text)
        self.assertNotIn("line 9\n", text)  # only the last 20 lines

    def test_status_without_installations(self) -> None:
        status_screen(self.ctx)
        self.assertIn("There are no installed applications yet", self.messages())

    def test_unhealthy_application_is_reported(self) -> None:
        self.run_install("0.1.0")
        self.reported_version = None
        status_screen(self.ctx)
        self.assertIn("not responding", self.messages())

    def test_logs_viewer(self) -> None:
        self.paths.logs_dir.mkdir(parents=True)
        (self.paths.logs_dir / "2026-11-18.log").write_text("old entry", encoding="utf-8")
        (self.paths.logs_dir / "2026-11-19.log").write_text("new entry", encoding="utf-8")
        self.ui.push("2026-11-18.log", UiBack)
        with self.assertRaises(UiBack):
            logs_screen(self.ctx)
        self.assertIn("old entry", self.messages())
        menu = next(c for c in self.ui.calls if c.kind == "menu")
        self.assertEqual(menu.items, ("2026-11-19.log", "2026-11-18.log"))  # newest first

    def test_no_logs_yet(self) -> None:
        logs_screen(self.ctx)
        self.assertIn("There are no logs yet", self.messages())


class SettingsFlowTests(AppTestCase):
    def test_setting_a_token_verifies_it_before_saving(self) -> None:
        self.tokens.delete()
        new = "github_pat_BRANDNEWTOKEN99"
        self.ui.push("token", new, REPO, UiBack)
        self.call(settings_menu)
        self.assertEqual(self.tokens.load(), new)
        self.assertIn("The token is valid", self.messages())
        self.assertEqual(self.log.redactor.redact(new), "***")

    def test_a_failing_token_is_not_saved(self) -> None:
        old = self.tokens.load()

        def deny(repo: str) -> None:
            raise InstallerError("No access to the repository")

        self.github.check_access = deny  # type: ignore[method-assign]
        self.ui.push("token", "github_pat_WRONGTOKEN12345", REPO, UiBack)
        self.call(settings_menu)
        self.assertEqual(self.tokens.load(), old)
        self.assertIn("No access to the repository", self.messages())

    def test_known_repos_can_be_added_and_removed(self) -> None:
        self.ui.push("repos", "__add__", "o/other", "/var/www/other", UiBack, UiBack)
        self.call(settings_menu)
        self.assertEqual(
            load_repos(self.paths.repos_file), [KnownRepo("o/other", "/var/www/other")]
        )
        self.ui.push("repos", "o/other", True, UiBack, UiBack)
        self.call(settings_menu)
        self.assertEqual(load_repos(self.paths.repos_file), [])

    def test_duplicate_repo_is_refused(self) -> None:
        save_repos(self.paths.repos_file, [KnownRepo("o/other")])
        self.ui.push("repos", "__add__", "o/other", UiBack, UiBack)
        self.call(settings_menu)
        self.assertIn("already on the list", self.messages())
        self.assertEqual(len(load_repos(self.paths.repos_file)), 1)

    def test_invalid_repo_name_is_asked_again(self) -> None:
        self.ui.push("repos", "__add__", "not a repo", "o/ok", "", UiBack, UiBack)
        self.call(settings_menu)
        self.assertEqual([k.repo for k in load_repos(self.paths.repos_file)], ["o/ok"])


if __name__ == "__main__":
    unittest.main()
