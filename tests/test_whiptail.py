import io
import subprocess
import unittest
from pathlib import Path
from typing import Any

from telepito.errors import PrerequisiteError
from telepito.ui.base import Item, UiBack
from telepito.ui.whiptail import BACKTITLE, WhiptailUi


class FakeRunner:
    """Stands in for ``subprocess.run``: records the command, returns canned results."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.results: list[tuple[int, bytes]] = []
        self.on_call: Any = None

    def push(self, code: int, stderr: str = "") -> None:
        self.results.append((code, stderr.encode("utf-8")))

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        self.kwargs.append(kwargs)
        if self.on_call is not None:
            self.on_call(command)
        code, err = self.results.pop(0)
        return subprocess.CompletedProcess(command, code, b"", err)


class _Pipe:
    """A write-only pipe that remembers what was written even after it was closed."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> int:
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdin: Any = _Pipe()
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return 0


ITEMS = [Item("a", "Alma"), Item("b", "Barack", "gyümölcs")]


class WhiptailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner()
        self.ui = WhiptailUi(runner=self.runner, terminal=lambda: (100, 30))

    @property
    def command(self) -> list[str]:
        return self.runner.calls[-1]


class MenuTests(WhiptailTestCase):
    def test_menu_arguments_and_result(self) -> None:
        self.runner.push(0, "b\n")
        self.assertEqual(self.ui.menu("Cím", ITEMS, text="Válassz", default="b"), "b")
        cmd = self.command
        self.assertEqual(cmd[:3], ["whiptail", "--backtitle", BACKTITLE])
        self.assertIn("--notags", cmd)
        self.assertEqual(cmd[cmd.index("--default-item") + 1], "b")
        tail = cmd[cmd.index("--menu") :]
        self.assertEqual(tail[1], "Válassz")
        self.assertEqual(tail[5:], ["a", "Alma", "b", "Barack - gyümölcs"])

    def test_cancel_and_escape_go_back(self) -> None:
        for code in (1, 255):
            self.runner.push(code)
            with self.subTest(code=code), self.assertRaises(UiBack):
                self.ui.menu("Cím", ITEMS)

    def test_unknown_default_is_ignored(self) -> None:
        self.runner.push(0, "a")
        self.ui.menu("Cím", ITEMS, default="zzz")
        self.assertNotIn("--default-item", self.command)

    def test_dialog_stays_inside_the_terminal(self) -> None:
        many = [Item(str(i), f"item {i}") for i in range(200)]
        self.runner.push(0, "1")
        ui = WhiptailUi(runner=self.runner, terminal=lambda: (80, 24))
        ui.menu("Cím", many, text="x")
        cmd = self.command
        i = cmd.index("--menu")
        height, width, rows = (int(v) for v in cmd[i + 2 : i + 5])
        self.assertLessEqual(height, 22)
        self.assertLessEqual(width, 74)
        self.assertLessEqual(rows, height)

    def test_leading_dash_in_text_is_not_an_option(self) -> None:
        self.runner.push(0, "a")
        self.ui.menu("Cím", ITEMS, text="--help")
        self.assertEqual(self.command[self.command.index("--menu") + 1], " --help")


class ChoiceTests(WhiptailTestCase):
    def test_radiolist_marks_the_default(self) -> None:
        self.runner.push(0, "b")
        self.assertEqual(self.ui.choose("Cím", ITEMS, default="b"), "b")
        cmd = self.command
        tail = cmd[cmd.index("--radiolist") + 5 :]
        self.assertEqual(tail, ["a", "Alma", "OFF", "b", "Barack - gyümölcs", "ON"])

    def test_radiolist_defaults_to_the_selected_item_then_the_first(self) -> None:
        self.runner.push(0, "a")
        self.ui.choose("Cím", [Item("a", "A"), Item("b", "B", selected=True)])
        tail = self.command[self.command.index("--radiolist") + 5 :]
        self.assertEqual(tail[2::3], ["OFF", "ON"])

    def test_checklist_parses_one_value_per_line(self) -> None:
        self.runner.push(0, "a\nb\n")
        self.assertEqual(self.ui.choose_many("Cím", ITEMS), ["a", "b"])
        self.assertIn("--separate-output", self.command)

    def test_empty_checklist_selection(self) -> None:
        self.runner.push(0, "")
        self.assertEqual(self.ui.choose_many("Cím", ITEMS), [])


class InputTests(WhiptailTestCase):
    def test_text_with_default(self) -> None:
        self.runner.push(0, "hello world\n")
        self.assertEqual(self.ui.text("Kérdés", title="T", default="alap"), "hello world")
        cmd = self.command
        i = cmd.index("--inputbox")
        self.assertEqual(cmd[i + 1], "Kérdés")
        self.assertEqual(cmd[-1], "alap")

    def test_text_cancel(self) -> None:
        self.runner.push(1)
        with self.assertRaises(UiBack):
            self.ui.text("Kérdés")

    def test_secret_never_appears_in_arguments(self) -> None:
        self.runner.push(0, "s3cr3t-value\n")
        self.assertEqual(self.ui.secret("Jelszó:"), "s3cr3t-value")
        self.assertIn("--passwordbox", self.command)
        self.assertNotIn("s3cr3t-value", " ".join(self.command))

    def test_secret_keeps_inner_whitespace(self) -> None:
        self.runner.push(0, " a b \n")
        self.assertEqual(self.ui.secret("Jelszó:"), " a b ")

    def test_secret_cancel(self) -> None:
        self.runner.push(255)
        with self.assertRaises(UiBack):
            self.ui.secret("Jelszó:")


class ConfirmTests(WhiptailTestCase):
    def test_yes_no_escape(self) -> None:
        self.runner.push(0)
        self.runner.push(1)
        self.runner.push(255)
        self.assertTrue(self.ui.confirm("Biztos?"))
        self.assertFalse(self.ui.confirm("Biztos?"))
        with self.assertRaises(UiBack):
            self.ui.confirm("Biztos?")

    def test_default_no_is_the_safe_default(self) -> None:
        self.runner.push(0)
        self.ui.confirm("Biztos?")
        self.assertIn("--defaultno", self.command)
        self.runner.push(0)
        self.ui.confirm("Biztos?", default=True)
        self.assertNotIn("--defaultno", self.command)

    def test_hungarian_buttons(self) -> None:
        self.runner.push(0)
        self.ui.confirm("Biztos?")
        cmd = self.command
        self.assertEqual(cmd[cmd.index("--yes-button") + 1], "Igen")
        self.assertEqual(cmd[cmd.index("--no-button") + 1], "Nem")


class MessageTests(WhiptailTestCase):
    def test_plain_message(self) -> None:
        self.runner.push(0)
        self.ui.message("Kész", title="T")
        self.assertIn("--msgbox", self.command)

    def test_escape_on_a_message_is_harmless(self) -> None:
        self.runner.push(255)
        self.ui.message("Kész")

    def test_scrollable_text_goes_through_a_temporary_file_that_is_removed(self) -> None:
        seen: dict[str, str] = {}

        def inspect(command: list[str]) -> None:
            path = Path(command[command.index("--textbox") + 1])
            seen["content"] = path.read_text(encoding="utf-8")
            seen["path"] = str(path)

        self.runner.on_call = inspect
        self.runner.push(0)
        self.ui.message("sor 1\nsor 2", title="T", scroll=True)
        self.assertEqual(seen["content"], "sor 1\nsor 2")
        self.assertIn("--scrolltext", self.command)
        self.assertFalse(Path(seen["path"]).exists())

    def test_huge_text_is_truncated_to_its_tail(self) -> None:
        captured: dict[str, int] = {}

        def inspect(command: list[str]) -> None:
            path = Path(command[command.index("--textbox") + 1])
            captured["size"] = len(path.read_text(encoding="utf-8"))

        self.runner.on_call = inspect
        self.runner.push(0)
        self.ui.message("x" * 1_000_000, scroll=True)
        self.assertLess(captured["size"], 450_000)


class ProgressTests(WhiptailTestCase):
    def test_gauge_receives_percent_and_text(self) -> None:
        process = FakeProcess()
        commands: list[list[str]] = []

        def popen(command: list[str], **kwargs: Any) -> FakeProcess:
            commands.append(command)
            return process

        ui = WhiptailUi(runner=self.runner, popen=popen, terminal=lambda: (100, 30))  # type: ignore[arg-type]
        with ui.progress("Letöltés", "kezdés") as bar:
            bar.update(40, "fél úton")
            bar.update(250)  # clamped
        self.assertIn("--gauge", commands[0])
        written = process.stdin.data.decode()
        self.assertIn("XXX\n40\nfél úton\nXXX\n", written)
        self.assertIn("XXX\n100\n\nXXX\n", written)
        self.assertTrue(process.waited)

    def test_updating_after_the_gauge_closed_is_ignored(self) -> None:
        class Broken(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.stdin = _ClosedPipe()  # type: ignore[assignment]

        ui = WhiptailUi(popen=lambda *a, **k: Broken(), terminal=lambda: (100, 30))  # type: ignore[arg-type, return-value]
        with ui.progress("T") as bar:
            bar.update(10)


class _ClosedPipe(io.BytesIO):
    def write(self, data: Any) -> int:
        raise BrokenPipeError

    def close(self) -> None:
        super().close()


class MissingWhiptailTests(unittest.TestCase):
    def test_runner_error_becomes_a_prerequisite_error(self) -> None:
        def runner(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError

        ui = WhiptailUi(runner=runner, terminal=lambda: (80, 24))
        with self.assertRaises(PrerequisiteError) as ctx:
            ui.menu("Cím", ITEMS)
        self.assertIn("apt install whiptail", str(ctx.exception))

    def test_progress_without_whiptail(self) -> None:
        def popen(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError

        ui = WhiptailUi(popen=popen, terminal=lambda: (80, 24))
        with self.assertRaises(PrerequisiteError), ui.progress("T"):
            pass


if __name__ == "__main__":
    unittest.main()
