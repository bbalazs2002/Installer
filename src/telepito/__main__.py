"""Allows ``python -m telepito`` (and the zipapp entry point)."""

import sys

from .cli import main

sys.exit(main())
