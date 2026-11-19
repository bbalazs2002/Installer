"""The real terminal interface, built on the ``whiptail`` command.

``whiptail`` draws on the terminal (inherited stdin/stdout) and reports the user's choice on
stderr, which is captured here. Exit status 0 is OK/Yes, 1 is Cancel/No and 255 is Esc; for
everything except a yes/no question both Cancel and Esc mean "back" and raise
:class:`~installer.ui.base.UiBack`. Secrets typed into a password box come back through the
stderr pipe and never appear in an argument list.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from ..errors import PrerequisiteError
from .base import Item, ProgressHandle, Ui, UiBack

BACKTITLE = "Telepítő"
CANCEL_LABEL = "Vissza"
_ESC = 255
_OVERHEAD = 8  # border, title, buttons and padding rows of a dialog
_MIN_WIDTH = 40
_MAX_WIDTH = 100
_TEXTBOX_LIMIT = 400_000  # characters shown in a scrollable box


def _terminal() -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


class _Gauge(ProgressHandle):
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def update(self, percent: int, text: str | None = None) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.closed:
            return
        percent = max(0, min(100, int(percent)))
        lines = ["XXX", str(percent), text if text is not None else "", "XXX"]
        try:
            stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
            stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # the user closed the gauge; nothing sensible left to update


class WhiptailUi(Ui):
    def __init__(
        self,
        *,
        executable: str = "whiptail",
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        terminal: Callable[[], tuple[int, int]] = _terminal,
    ) -> None:
        self._exe = executable
        self._runner = runner
        self._popen = popen
        self._terminal = terminal

    @staticmethod
    def available() -> bool:
        return shutil.which("whiptail") is not None

    # ------------------------------------------------------------------ plumbing

    def _geometry(self, text: str, rows: int = 0) -> tuple[int, int]:
        """(height, width) for a dialog showing ``text`` plus ``rows`` list rows."""
        columns, lines = self._terminal()
        width = max(_MIN_WIDTH, min(columns - 6, _MAX_WIDTH))
        wrapped = 0
        for paragraph in text.split("\n") or [""]:
            wrapped += max(1, len(textwrap.wrap(paragraph, width - 6)) or 1)
        height = min(max(lines - 2, 10), wrapped + rows + _OVERHEAD)
        return height, width

    def _list_rows(self, text: str, count: int) -> tuple[int, int, int]:
        """(height, width, visible list rows) for a menu/radiolist/checklist."""
        _, lines = self._terminal()
        columns, _ = self._terminal()
        width = max(_MIN_WIDTH, min(columns - 6, _MAX_WIDTH))
        text_rows = sum(max(1, len(textwrap.wrap(p, width - 6)) or 1) for p in text.split("\n"))
        available = max(3, lines - 2 - _OVERHEAD - text_rows)
        visible = max(1, min(count, available))
        return text_rows + visible + _OVERHEAD, width, visible

    def _call(self, args: Sequence[str]) -> tuple[int, str]:
        command = [self._exe, "--backtitle", BACKTITLE, *args]
        try:
            completed = self._runner(command, stderr=subprocess.PIPE, check=False)
        except FileNotFoundError as exc:
            raise PrerequisiteError(
                "A whiptail nem található. Telepítés: sudo apt install whiptail"
            ) from exc
        output = (completed.stderr or b"").decode("utf-8", errors="replace")
        return completed.returncode, output

    @staticmethod
    def _text(text: str) -> str:
        text = text or " "
        # whiptail would read a leading dash as an option
        return " " + text if text.startswith("-") else text

    @staticmethod
    def _label(item: Item) -> str:
        return f"{item.label} - {item.description}" if item.description else item.label

    @staticmethod
    def _buttons() -> list[str]:
        return ["--ok-button", "OK", "--cancel-button", CANCEL_LABEL]

    # ---------------------------------------------------------------------- Ui

    def menu(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        text = self._text(text)
        height, width, rows = self._list_rows(text, len(items))
        args = ["--title", title, "--notags", *self._buttons()]
        if default is not None and any(i.key == default for i in items):
            args += ["--default-item", default]
        args += ["--menu", text, str(height), str(width), str(rows)]
        for item in items:
            args += [item.key, self._label(item)]
        code, output = self._call(args)
        if code != 0:
            raise UiBack()
        return output.strip()

    def choose(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        text = self._text(text)
        height, width, rows = self._list_rows(text, len(items))
        args = ["--title", title, "--notags", *self._buttons(), "--radiolist"]
        args += [text, str(height), str(width), str(rows)]
        chosen = (
            default
            if default is not None
            else next((i.key for i in items if i.selected), items[0].key if items else None)
        )
        for item in items:
            args += [item.key, self._label(item), "ON" if item.key == chosen else "OFF"]
        code, output = self._call(args)
        if code != 0:
            raise UiBack()
        return output.strip()

    def choose_many(self, title: str, items: Sequence[Item], *, text: str = "") -> list[str]:
        text = self._text(text)
        height, width, rows = self._list_rows(text, len(items))
        args = ["--title", title, "--notags", "--separate-output", *self._buttons(), "--checklist"]
        args += [text, str(height), str(width), str(rows)]
        for item in items:
            args += [item.key, self._label(item), "ON" if item.selected else "OFF"]
        code, output = self._call(args)
        if code != 0:
            raise UiBack()
        return [line for line in output.splitlines() if line.strip()]

    def text(self, prompt: str, *, title: str = "", default: str = "") -> str:
        prompt = self._text(prompt)
        height, width = self._geometry(prompt, 1)
        args = ["--title", title, *self._buttons(), "--inputbox", prompt, str(height), str(width)]
        args.append(default)
        code, output = self._call(args)
        if code != 0:
            raise UiBack()
        return output.rstrip("\n")

    def secret(self, prompt: str, *, title: str = "") -> str:
        prompt = self._text(prompt)
        height, width = self._geometry(prompt, 1)
        args = ["--title", title, *self._buttons(), "--passwordbox", prompt]
        args += [str(height), str(width)]
        code, output = self._call(args)
        if code != 0:
            raise UiBack()
        return output.rstrip("\n")

    def confirm(self, prompt: str, *, title: str = "", default: bool = False) -> bool:
        prompt = self._text(prompt)
        height, width = self._geometry(prompt)
        args = ["--title", title, "--yes-button", "Igen", "--no-button", "Nem"]
        if not default:
            args.append("--defaultno")
        args += ["--yesno", prompt, str(height), str(width)]
        code, _ = self._call(args)
        if code == 0:
            return True
        if code == _ESC:
            raise UiBack()
        return False

    def message(self, text: str, *, title: str = "", scroll: bool = False) -> None:
        text = self._text(text)
        if not scroll:
            height, width = self._geometry(text)
            self._call(
                ["--title", title, "--ok-button", "OK", "--msgbox", text, str(height), str(width)]
            )
            return
        columns, lines = self._terminal()
        width = max(_MIN_WIDTH, min(columns - 4, 120))
        shown = text if len(text) <= _TEXTBOX_LIMIT else "...\n" + text[-_TEXTBOX_LIMIT:]
        fd, name = tempfile.mkstemp(prefix="tp-text-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(shown)
            self._call(
                [
                    "--title",
                    title,
                    "--scrolltext",
                    "--ok-button",
                    "OK",
                    "--textbox",
                    name,
                    str(max(lines - 2, 10)),
                    str(width),
                ]  # fmt: skip
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)

    @contextlib.contextmanager
    def _progress(self, title: str, text: str) -> Iterator[ProgressHandle]:
        text = self._text(text)
        height, width = self._geometry(text, 1)
        command = [self._exe, "--backtitle", BACKTITLE, "--title", title]
        command += ["--gauge", text, str(height), str(width), "0"]
        try:
            process: Any = self._popen(command, stdin=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise PrerequisiteError(
                "A whiptail nem található. Telepítés: sudo apt install whiptail"
            ) from exc
        try:
            yield _Gauge(process)
        finally:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            process.wait()

    def progress(
        self, title: str, text: str = ""
    ) -> contextlib.AbstractContextManager[ProgressHandle]:
        return self._progress(title, text)
