#!/usr/bin/env python3
"""Build ``telepito.pyz``, the single-file zipapp that runs on the server.

Usage: ``python scripts/build_pyz.py [output]`` (default ``dist/telepito.pyz``).

The archive holds only the ``installer`` package (no tests, no caches) and a ``__main__.py``
that passes ``main()``'s return value to ``sys.exit`` (``zipapp``'s own ``-m`` template would
drop the exit code). The shebang is ``/usr/bin/env python3``; the file is made executable.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "installer"
MAIN = "import sys\n\nfrom installer.cli import main\n\nsys.exit(main())\n"
SHEBANG = "/usr/bin/env python3"


def build(output: Path) -> Path:
    """Create the archive at ``output`` and return its path."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyz-build-") as tmp:
        staging = Path(tmp) / "app"
        shutil.copytree(
            PACKAGE,
            staging / "installer",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        (staging / "__main__.py").write_text(MAIN, encoding="utf-8")
        if output.exists():
            output.unlink()
        zipapp.create_archive(staging, output, interpreter=SHEBANG, compressed=True)
    return output


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else ROOT / "dist" / "telepito.pyz"
    print(build(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
