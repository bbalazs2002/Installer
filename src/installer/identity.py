"""The program's name, and the name it was known by before 0.2.0.

Until 0.1.x the program was called ``telepito`` (the command, the ``telepito.pyz`` file and the
``telepito <version>`` output of ``--version``). Installers of that generation update themselves
by downloading the release asset ``telepito.pyz``, running it with ``--version`` and requiring
the output ``telepito <version>``. To let them upgrade to a release that is called ``installer``,
the release carries the same archive under both names, and the program reports the name of the
*file it runs from*: ``installer`` normally, ``telepito`` only when that file is still called
``telepito.pyz`` (which is exactly the situation of a 0.1.x updater testing the download).
"""

from __future__ import annotations

from pathlib import Path

PROGRAM = "installer"
PYZ_NAME = "installer.pyz"
LEGACY_PROGRAM = "telepito"
LEGACY_PYZ_NAME = "telepito.pyz"


def program_name(pyz: Path | None) -> str:
    """``telepito`` if running from a file still named ``telepito.pyz``, else ``installer``."""
    return LEGACY_PROGRAM if pyz is not None and pyz.name == LEGACY_PYZ_NAME else PROGRAM
