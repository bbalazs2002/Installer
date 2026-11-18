"""Command-line entry point. In T.0 only ``--version`` is functional."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .errors import EXIT_ERROR, EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telepito",
        description="Telepítő GitHub-release-ekhez (Docker).",
    )
    parser.add_argument("--version", action="version", version=f"telepito {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    print("A menü még nincs kész (T.0 fázis).", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["EXIT_OK", "build_parser", "main"]
