import unittest

from installer.ui.base import Item, UiBack, ask_validated
from installer.ui.scripted import ScriptedUi

ITEMS = [Item("a", "Apple"), Item("b", "Peach")]


class ScriptedUiTests(unittest.TestCase):
    def test_answers_are_played_back_in_order(self) -> None:
        ui = ScriptedUi(["b", "a", ["a", "b"], "hello", "s3cret", True])
        self.assertEqual(ui.menu("M", ITEMS), "b")
        self.assertEqual(ui.choose("C", ITEMS), "a")
        self.assertEqual(ui.choose_many("CM", ITEMS), ["a", "b"])
        self.assertEqual(ui.text("t"), "hello")
        self.assertEqual(ui.secret("s"), "s3cret")
        self.assertTrue(ui.confirm("ok?"))
        self.assertEqual(ui.remaining, 0)

    def test_calls_are_recorded_without_answers(self) -> None:
        ui = ScriptedUi(["pw"])
        ui.secret("Password:")
        ui.message("Done", title="T")
        self.assertEqual([c.kind for c in ui.calls], ["secret", "message"])
        self.assertNotIn("pw", repr(ui.calls))
        self.assertEqual(ui.messages(), ["Done"])

    def test_back_class_and_instance(self) -> None:
        ui = ScriptedUi([UiBack, UiBack()])
        with self.assertRaises(UiBack):
            ui.menu("M", ITEMS)
        with self.assertRaises(UiBack):
            ui.confirm("?")

    def test_unknown_key_and_exhaustion_fail_loudly(self) -> None:
        with self.assertRaises(AssertionError):
            ScriptedUi(["zzz"]).menu("M", ITEMS)
        with self.assertRaises(AssertionError):
            ScriptedUi().text("x")
        with self.assertRaises(AssertionError):
            ScriptedUi([1]).confirm("x")

    def test_progress_records_updates(self) -> None:
        ui = ScriptedUi()
        with ui.progress("Download") as bar:
            bar.update(10)
            bar.update(100, "done")
        self.assertEqual(ui.progress_updates, [(10, None), (100, "done")])

    def test_ask_validated_repeats_until_valid(self) -> None:
        ui = ScriptedUi(["bad", "also bad", "good"])
        result = ask_validated(ui, "name?", lambda v: None if v == "good" else "invalid")
        self.assertEqual(result, "good")
        self.assertEqual(ui.messages(), ["invalid", "invalid"])

    def test_ask_validated_secret_uses_secret_prompt_only(self) -> None:
        ui = ScriptedUi(["x", "longenough"])
        result = ask_validated(
            ui, "password", lambda v: None if len(v) >= 5 else "too short", secret=True
        )
        self.assertEqual(result, "longenough")
        self.assertTrue(all(c.kind in ("secret", "message") for c in ui.calls))


if __name__ == "__main__":
    unittest.main()
