"""Drives the real ``whiptail`` through a pseudo-terminal (Linux only).

These tests prove that the argument lists built by :class:`WhiptailUi` really work with the
installed whiptail: sizes, ``--notags``, return codes, the stderr result channel and the gauge.
Skipped on Windows and wherever whiptail is not installed.
"""

import json
import os
import select
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows has none of these
    pty = None  # type: ignore[assignment]


def _can_run() -> bool:
    return sys.platform != "win32" and pty is not None and shutil.which("whiptail") is not None


def drive(
    code: str, keys: list[tuple[bytes, float]], *, timeout: float = 20.0
) -> tuple[int, object]:
    """Run ``code`` (Python source) on a pty, feeding ``keys``; return (exit code, result).

    ``code`` must write its result as JSON to the path in the ``OUT`` variable. Each key
    entry is ``(bytes, seconds_to_wait_before_sending)``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "result.json")
        program = f"OUT = {out!r}\nimport sys\nsys.path.insert(0, {SRC!r})\n{code}"
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.environ["TERM"] = "xterm"
            os.environ["NEWT_COLORS"] = ""
            os.execv(sys.executable, [sys.executable, "-c", program])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        deadline = time.monotonic() + timeout
        pending = list(keys)
        next_send = time.monotonic() + (pending[0][1] if pending else 0)
        status = None
        captured = b""
        eof = False
        while time.monotonic() < deadline:
            if not eof:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:  # EIO: the child closed its side
                        chunk = b""
                    if chunk:
                        captured += chunk
                    else:
                        eof = True
            else:
                time.sleep(0.05)
            if pending and time.monotonic() >= next_send:
                os.write(fd, pending.pop(0)[0])
                if pending:
                    next_send = time.monotonic() + pending[0][1]
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
        if status is None:
            os.kill(pid, 9)
            _, status = os.waitpid(pid, 0)
            raise AssertionError(
                "A whiptail-teszt időtúllépéssel leállt. Képernyő:\n"
                + captured.decode("utf-8", errors="replace")[-1500:]
            )
        os.close(fd)
        result = json.loads(Path(out).read_text()) if os.path.exists(out) else None
        return os.waitstatus_to_exitcode(status), result


PRELUDE = """
import json
from telepito.ui.base import Item, UiBack
from telepito.ui.whiptail import WhiptailUi
ui = WhiptailUi()
def finish(value):
    open(OUT, "w").write(json.dumps(value))
"""

ENTER, DOWN, TAB = b"\r", b"\x1b[B", b"\t"


@unittest.skipUnless(_can_run(), "needs Linux and whiptail")
class RealWhiptailTests(unittest.TestCase):
    def test_menu_selects_the_second_entry(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.menu("Cím", [Item("a", "Alma"), Item("b", "Barack", "leírás")], text="Válassz"))
"""
        )
        status, result = drive(code, [(DOWN, 1.0), (ENTER, 0.3)])
        self.assertEqual((status, result), (0, "b"))

    def test_menu_default_item(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.menu("Cím", [Item("a", "Alma"), Item("b", "Barack")], default="b"))
"""
        )
        self.assertEqual(drive(code, [(ENTER, 1.0)]), (0, "b"))

    def test_cancel_button_means_back(self) -> None:
        code = (
            PRELUDE
            + """
try:
    finish(ui.menu("Cím", [Item("a", "Alma")]))
except UiBack:
    finish("BACK")
"""
        )
        self.assertEqual(drive(code, [(TAB, 1.0), (TAB, 0.3), (ENTER, 0.3)]), (0, "BACK"))

    def test_inputbox_returns_typed_text_with_accents(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.text("Mappa?", title="Cél"))
"""
        )
        status, result = drive(code, [("/var/www/árvíz".encode(), 1.0), (ENTER, 0.3)])
        self.assertEqual((status, result), (0, "/var/www/árvíz"))

    def test_inputbox_keeps_its_default_on_enter(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.text("Port?", default="2570"))
"""
        )
        self.assertEqual(drive(code, [(ENTER, 1.0)]), (0, "2570"))

    def test_passwordbox_returns_the_secret(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.secret("Jelszó:"))
"""
        )
        self.assertEqual(drive(code, [(b"pw 123", 1.0), (ENTER, 0.3)]), (0, "pw 123"))

    def test_yes_no_answers(self) -> None:
        code = (
            PRELUDE
            + """
finish([ui.confirm("Biztos?", default=True), ui.confirm("Biztos?", default=False)])
"""
        )
        # default True: Enter = Igen; default False: Enter = Nem
        self.assertEqual(drive(code, [(ENTER, 1.0), (ENTER, 1.0)]), (0, [True, False]))

    def test_radiolist_and_checklist(self) -> None:
        code = (
            PRELUDE
            + """
items = [Item("a", "Alma"), Item("b", "Barack"), Item("c", "Cseresznye")]
one = ui.choose("Egy", items, default="a")
many = ui.choose_many("Több", items)
finish([one, many])
"""
        )
        keys = [
            (DOWN, 1.0), (b" ", 0.2), (ENTER, 0.2),  # radiolist: move to b, select, OK
            (b" ", 1.0), (DOWN, 0.2), (DOWN, 0.2), (b" ", 0.2), (ENTER, 0.2),  # a and c
        ]  # fmt: skip
        self.assertEqual(drive(code, keys), (0, ["b", ["a", "c"]]))

    def test_message_and_scrollable_text(self) -> None:
        code = (
            PRELUDE
            + """
ui.message("Kész", title="T")
ui.message("\\n".join(f"sor {i}" for i in range(200)), title="Napló", scroll=True)
finish("ok")
"""
        )
        # In the scrollable text box focus starts in the text: Tab moves it to the OK button.
        self.assertEqual(drive(code, [(ENTER, 1.0), (TAB, 1.0), (ENTER, 0.3)]), (0, "ok"))

    def test_gauge(self) -> None:
        code = (
            PRELUDE
            + """
import time
with ui.progress("Letöltés", "indul") as bar:
    for percent in (10, 50, 100):
        bar.update(percent, f"{percent}%")
        time.sleep(0.2)
finish("done")
"""
        )
        self.assertEqual(drive(code, []), (0, "done"))

    def test_long_text_in_a_small_terminal_still_opens(self) -> None:
        code = (
            PRELUDE
            + """
finish(ui.menu("Cím", [Item(str(i), f"elem {i}") for i in range(100)], text="szöveg " * 40))
"""
        )
        self.assertEqual(drive(code, [(ENTER, 1.0)]), (0, "0"))


if __name__ == "__main__":
    unittest.main()
