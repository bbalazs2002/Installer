"""A ``Ui`` that plays back prepared answers. Used by tests to walk through whole flows."""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from .base import Item, ProgressHandle, Ui, UiBack


@dataclass(frozen=True)
class Call:
    """A recorded interaction, for assertions in tests. Answers are never recorded."""

    kind: str
    title: str
    text: str
    items: tuple[str, ...] = ()


class _Handle(ProgressHandle):
    def __init__(self, sink: list[tuple[int, str | None]]) -> None:
        self._sink = sink

    def update(self, percent: int, text: str | None = None) -> None:
        self._sink.append((percent, text))


class _DefaultAnswer:
    """Marker: "the user just pressed Enter", i.e. accept the prefilled default text."""

    def __repr__(self) -> str:
        return "DEFAULT"


class ScriptedUi(Ui):
    """Pops one prepared answer per question.

    Answers: ``str`` for menu/choose/text/secret, ``bool`` for confirm, ``list[str]`` for
    choose_many, and ``UiBack`` (class or instance) to simulate Esc. Messages and progress
    need no answer; they are only recorded.
    """

    DEFAULT = _DefaultAnswer()

    def __init__(self, answers: Sequence[Any] = ()) -> None:
        self._answers: deque[Any] = deque(answers)
        self.calls: list[Call] = []
        self.progress_updates: list[tuple[int, str | None]] = []

    def push(self, *answers: Any) -> None:
        self._answers.extend(answers)

    @property
    def remaining(self) -> int:
        return len(self._answers)

    def messages(self) -> list[str]:
        return [c.text for c in self.calls if c.kind == "message"]

    def _next(self, kind: str) -> Any:
        if not self._answers:
            raise AssertionError(f"ScriptedUi: nincs több előre megadott válasz ({kind})")
        answer = self._answers.popleft()
        if answer is UiBack or isinstance(answer, UiBack):
            raise UiBack()
        return answer

    def _pick(self, kind: str, items: Sequence[Item]) -> str:
        answer = self._next(kind)
        keys = [i.key for i in items]
        if not isinstance(answer, str) or answer not in keys:
            raise AssertionError(f"ScriptedUi: {answer!r} nem elem ({kind}): {keys}")
        return answer

    def menu(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        self.calls.append(Call("menu", title, text, tuple(i.key for i in items)))
        return self._pick("menu", items)

    def choose(
        self, title: str, items: Sequence[Item], *, text: str = "", default: str | None = None
    ) -> str:
        self.calls.append(Call("choose", title, text, tuple(i.key for i in items)))
        return self._pick("choose", items)

    def choose_many(self, title: str, items: Sequence[Item], *, text: str = "") -> list[str]:
        self.calls.append(Call("choose_many", title, text, tuple(i.key for i in items)))
        answer = self._next("choose_many")
        keys = {i.key for i in items}
        if not isinstance(answer, list) or not set(answer) <= keys:
            raise AssertionError(f"ScriptedUi: érvénytelen választás: {answer!r}")
        return list(answer)

    def text(self, prompt: str, *, title: str = "", default: str = "") -> str:
        self.calls.append(Call("text", title, prompt))
        answer = self._next("text")
        if answer is self.DEFAULT:
            return default
        if not isinstance(answer, str):
            raise AssertionError(f"ScriptedUi: szöveg kellene, kapott: {answer!r}")
        return answer

    def secret(self, prompt: str, *, title: str = "") -> str:
        self.calls.append(Call("secret", title, prompt))
        answer = self._next("secret")
        if not isinstance(answer, str):
            raise AssertionError("ScriptedUi: szöveg kellene a titokhoz")
        return answer

    def confirm(self, prompt: str, *, title: str = "", default: bool = False) -> bool:
        self.calls.append(Call("confirm", title, prompt))
        answer = self._next("confirm")
        if not isinstance(answer, bool):
            raise AssertionError(f"ScriptedUi: igaz/hamis kellene, kapott: {answer!r}")
        return answer

    def message(self, text: str, *, title: str = "", scroll: bool = False) -> None:
        self.calls.append(Call("message", title, text))

    @contextlib.contextmanager
    def _progress(self, title: str, text: str) -> Iterator[ProgressHandle]:
        self.calls.append(Call("progress", title, text))
        yield _Handle(self.progress_updates)

    def progress(
        self, title: str, text: str = ""
    ) -> contextlib.AbstractContextManager[ProgressHandle]:
        return self._progress(title, text)
