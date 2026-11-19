# Changelog

## 0.1.1

- The whole program is in English: the interface, messages, documentation and README.
- The Python package was renamed from `telepito` to `installer`. The program name (`telepito`),
  the `telepito.pyz` asset and the `telepito <version>` output of `--version` are unchanged, so
  installers deployed earlier can still update themselves.
- **The installer directory is no longer fixed.** By default the installer keeps its files
  (`installs.json`, `repos.json`, `vault/`, `logs/`, `tmp/`) in the directory that contains
  `telepito.pyz`; `--state-dir` and `TELEPITO_STATE_DIR` still override it.
- The GitHub token now lives in `~/.config/installer/token`. A token left by 0.1.0 in
  `~/.config/telepito/token` is still read if the new file does not exist.
- Downgrade confirmation word: `DOWNGRADE`.

## 0.1.0

The first release.

- Install, update, downgrade and recover from GitHub releases as Docker containers, with SHA-256
  verification, a review of compose file changes and a health check.
- A `whiptail` menu; read-only commands: `status`, `plan`, `updates`.
- An interactive `.env.production` wizard, an encrypted vault (scrypt, AES-256, HMAC) and secret
  rotation.
- Postgres: create the database and user (SCRAM-SHA-256), a backup before every update, restore,
  and deletion of backups older than a year.
- Self-update by replacing `telepito.pyz`.
