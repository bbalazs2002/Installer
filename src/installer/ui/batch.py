"""A ``Ui`` for the non-interactive commands (``status``, ``plan``, ``updates``).

Any attempt to ask the user something fails with a clear error: those commands must never
need an answer. Messages go to stderr, so stdout stays clean for the command's real output.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator, Sequence

from ..errors import TelepitoError
from .base import Item, ProgressHandle, Ui, null_progress


def _no_input() -> TelepitoError:
    return TelepitoError("Ehhez a művelethez interaktív felület kell (indítsd paraméter nélkül).")


class BatchUi(Ui):
    def menu(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        raise _no_input()

    def choose(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        raise _no_input()

    def choose_many(self, title: str, items: Sequence[Item], *, text: str = "") -> list[str]:
        raise _no_input()

    def text(self, prompt: str, *, title: str = "", default: str = "") -> str:
        raise _no_input()

    def secret(self, prompt: str, *, title: str = "") -> str:
        raise _no_input()

    def confirm(self, prompt: str, *, title: str = "", default: bool = False) -> bool:
        raise _no_input()

    def message(self, text: str, *, title: str = "", scroll: bool = False) -> None:
        print(text, file=sys.stderr)

    def progress(
        self, title: str, text: str = ""
    ) -> contextlib.AbstractContextManager[ProgressHandle]:
        return null_progress()


__all__ = ["BatchUi", "Iterator"]
