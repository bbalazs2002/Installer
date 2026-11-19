"""The ``Ui`` interface. Menus and flows only talk to this, never to whiptail directly."""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass


class UiBack(Exception):  # noqa: N818 - control-flow signal, not an error
    """The user pressed Esc / Cancel: go back one screen."""


@dataclass(frozen=True)
class Item:
    """One selectable entry. ``key`` is returned, ``label`` is shown."""

    key: str
    label: str
    description: str = ""
    selected: bool = False  # initial state for checklists / default for radiolists


class ProgressHandle(ABC):
    @abstractmethod
    def update(self, percent: int, text: str | None = None) -> None:
        """Set the gauge to ``percent`` (0..100), optionally changing the text."""


class Ui(ABC):
    """The spec's seven operations (plus ``choose_many`` for checklists).

    Esc/Cancel raises :class:`UiBack`.
    """

    @abstractmethod
    def menu(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        """Show a menu and return the chosen item's key."""

    @abstractmethod
    def choose(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        """Radiolist: exactly one item; returns its key."""

    @abstractmethod
    def choose_many(self, title: str, items: Sequence[Item], *, text: str = "") -> list[str]:
        """Checklist: zero or more items; returns their keys."""

    @abstractmethod
    def text(self, prompt: str, *, title: str = "", default: str = "") -> str:
        """Single-line text input."""

    @abstractmethod
    def secret(self, prompt: str, *, title: str = "") -> str:
        """Hidden input. The value must never be echoed or logged."""

    @abstractmethod
    def confirm(self, prompt: str, *, title: str = "", default: bool = False) -> bool:
        """Yes/No. ``No`` returns ``False``; Esc raises :class:`UiBack`."""

    @abstractmethod
    def message(self, text: str, *, title: str = "", scroll: bool = False) -> None:
        """Show a message (``scroll=True`` for long, pageable text such as logs or diffs)."""

    @abstractmethod
    def progress(
        self, title: str, text: str = ""
    ) -> contextlib.AbstractContextManager[ProgressHandle]:
        """Context manager yielding a :class:`ProgressHandle`."""


def ask_validated(
    ui: Ui,
    prompt: str,
    validator: Callable[[str], str | None],
    *,
    title: str = "",
    default: str = "",
    secret: bool = False,
) -> str:
    """Ask until ``validator`` returns ``None`` (valid) instead of an error message.

    The error is shown to the user, then the question is repeated. Esc raises ``UiBack``.
    """
    value = default
    while True:
        if secret:
            value = ui.secret(prompt, title=title)
        else:
            value = ui.text(prompt, title=title, default=value)
        error = validator(value)
        if error is None:
            return value
        ui.message(error, title=title or "Invalid value")


class _NullProgress(ProgressHandle):
    def update(self, percent: int, text: str | None = None) -> None:
        return None


@contextlib.contextmanager
def null_progress() -> Iterator[ProgressHandle]:
    """A progress context that does nothing (for non-interactive use)."""
    yield _NullProgress()
