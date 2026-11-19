"""Where the installer keeps its own files.

The state directory is *not* a fixed location: by default it is the directory that holds the
running ``telepito.pyz``, so wherever the installer is put, its files (``installs.json``,
``repos.json``, ``vault/``, ``logs/``, ``tmp/``, ``.lock``) live right next to it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PrerequisiteError
from .selfupdate import running_pyz

STATE_DIR_ENV = "TELEPITO_STATE_DIR"
TOKEN_FILE_ENV = "TELEPITO_TOKEN_FILE"
CONFIG_DIR_NAME = "installer"  # ~/.config/installer/token
LEGACY_CONFIG_DIR_NAME = "telepito"  # where 0.1.0 kept the token; still read if present


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    token_file: Path
    # The pre-0.1.1 token location; only set when the default location is in use.
    legacy_token_file: Path | None = None

    @property
    def repos_file(self) -> Path:
        return self.state_dir / "repos.json"

    @property
    def installs_file(self) -> Path:
        return self.state_dir / "installs.json"

    @property
    def vault_file(self) -> Path:
        return self.state_dir / "vault" / "vault.enc"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / ".lock"


def default_paths(
    state_dir: str | os.PathLike[str] | None = None,
    token_file: str | os.PathLike[str] | None = None,
) -> Paths:
    """Resolve the locations.

    State directory: ``--state-dir``, else ``$TELEPITO_STATE_DIR``, else the directory of the
    running ``telepito.pyz`` (symlinks resolved). Outside a zipapp (a source checkout) there is
    no sensible default, so one of the first two must be given.
    """
    state: str | os.PathLike[str] | None = state_dir or os.environ.get(STATE_DIR_ENV)
    if not state:
        pyz = running_pyz()
        if pyz is None:
            raise PrerequisiteError(
                "The installer's directory is unknown: this is not running from telepito.pyz. "
                f"Pass --state-dir or set {STATE_DIR_ENV}."
            )
        state = pyz.parent
    explicit = token_file or os.environ.get(TOKEN_FILE_ENV)
    if explicit:
        return Paths(Path(state), Path(explicit))
    config = Path.home() / ".config"
    return Paths(
        Path(state),
        config / CONFIG_DIR_NAME / "token",
        legacy_token_file=config / LEGACY_CONFIG_DIR_NAME / "token",
    )
