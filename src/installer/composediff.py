"""Review of a changed ``docker-compose.deploy.yml`` (specification 6.5). Pure logic.

The YAML is *not* parsed (no external library). Instead the text difference is shown, and
lines that were **added or modified** (``+`` lines of the diff) and contain a risky keyword are
highlighted with ``!!``. Unchanged and removed lines never trigger a warning. A ``build:`` key
anywhere in the new file is a hard blocker, because the release contract forbids it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

RISKY_KEYWORDS = (
    "volumes",
    "volumes_from",
    "privileged",
    "network_mode",
    "cap_add",
    "devices",
    "pid",
    "ipc",
    "userns_mode",
    "security_opt",
    "ports",
    "command",
    "entrypoint",
    "user",
    "extra_hosts",
    "build",
    "/var/run/docker.sock",
)

_WORD = r"(?<![A-Za-z0-9_])"
_RISKY_RE = re.compile(
    "|".join(
        _WORD + re.escape(word) + r"(?![A-Za-z0-9_])"
        for word in sorted(RISKY_KEYWORDS, key=len, reverse=True)
    )
)
# A changed list item that publishes a port on anything but loopback (e.g. "0.0.0.0:80:80",
# "8080:80", "${PORT}:${PORT}") does not contain the word ``ports`` itself, but is exactly the
# kind of change a reviewer must see.
_PORT = r"(?:\d+|\$\{[^}]+\})"
_EXPOSED_PORT_RE = re.compile(
    r"^\s*-\s*[\"']?(?!127\.0\.0\.1:)(?:(?:\d{1,3}\.){3}\d{1,3}:|\[[:0-9a-fA-F]*\]:)?"
    + _PORT
    + r"(?::"
    + _PORT
    + r"){1,2}(?:/\w+)?[\"']?\s*$"
)
_BUILD_KEY_RE = re.compile(r"^\s*(?:-\s*)?build\s*:")
FLAG = "!!"


@dataclass(frozen=True)
class ComposeReview:
    diff: str  # the unified diff with ``!!`` markers; empty if nothing changed
    flagged: tuple[str, ...]  # the highlighted added/modified lines (without the "+")
    build_present: bool  # a ``build:`` key in the new file (blocks the installation)

    @property
    def identical(self) -> bool:
        return not self.diff

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.flagged)


def has_build_key(text: str) -> bool:
    return any(
        _BUILD_KEY_RE.match(line) for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def review_compose(old: str | None, new: str) -> ComposeReview:
    """Compare the installed (``old``) and the new compose file.

    ``old=None`` (a new installation) shows no diff; only the ``build:`` check applies.
    """
    build = has_build_key(new)
    if old is None:
        return ComposeReview("", (), build)
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="installed",
        tofile="new",
        lineterm="",
    )
    out: list[str] = []
    flagged: list[str] = []
    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if _RISKY_RE.search(content) or _EXPOSED_PORT_RE.match(content):
                out.append(f"{FLAG} {line}")
                flagged.append(content.strip())
                continue
        out.append(line)
    return ComposeReview("\n".join(out), tuple(flagged), build)
