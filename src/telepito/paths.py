"""Where the installer keeps its own files (specification 4)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = "/var/www/telepito"
STATE_DIR_ENV = "TELEPITO_STATE_DIR"
TOKEN_FILE_ENV = "TELEPITO_TOKEN_FILE"


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    token_file: Path

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
    """Resolve the locations; arguments beat environment variables beat the defaults."""
    state = state_dir or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR
    token = (
        token_file
        or os.environ.get(TOKEN_FILE_ENV)
        or Path.home() / ".config" / "telepito" / "token"
    )
    return Paths(Path(state), Path(token))
