# Changelog

## 0.2.0

- **Everything is called `installer` now**: the file (`installer.pyz`), the command, the
  `--version` output (`installer <version>`), the environment variables (`INSTALLER_STATE_DIR`,
  `INSTALLER_TOKEN_FILE`; the `TELEPITO_*` names still work), the vault file header and the
  internal names. The self-update downloads the asset `installer.pyz`.
- **Upgrading from 0.1.x keeps working.** Each release also carries the identical archive as
  `telepito.pyz`, which a 0.1.x updater needs; run from a file with that name the program reports
  itself as `telepito <version>`, as those updaters require. On its first interactive start such
  a file renames itself to `installer.pyz` and leaves a `telepito.pyz` symlink behind.
- Vaults written by 0.1.x (header `TELEPITO-VAULT`) still open and are rewritten with the new
  header on the next save.
- **Data volumes.** `deploy.json` may declare `volumes` (named volumes, or bind mounts below
  `${DATA_DIR}`). The compose file is checked through `docker compose config --format json`:
  undeclared mounts, `docker.sock`, other host paths, `external` or renamed volumes stop the
  installation before anything is changed, and a new installation shows the volumes and the
  risky settings and asks once.
- **Volume backups.** Before every update, downgrade or reinstall each volume with
  `backup: true` is archived as `<volume>-<version>-<time>.tar.gz` (mode 600, read back to
  verify) together with the database dump, which now share one time stamp. The application is
  stopped while the volumes are copied and started again if the operation is cancelled. The
  plan, the free-space check and the status screen know about the volumes.
- **Restore and recovery.** The menu entry *Database* became *Backups*: back up now, restore any
  part of any backup set (database and/or volumes, a safety copy is made first and the
  application name must be typed), delete old backups. When a new release does not start, the
  recovery menu can restore the volumes together with the database.
- **Retention changed:** backups older than a year are still deleted, but the newest backup of
  the database and of each volume is always kept, however old (also for applications that only
  have a database).
- `proxy.snippet` may use `{basePath}`, replaced by the installation's `BASE_PATH`.
- The compose review no longer flags `user: "${APP_UID}:${APP_GID}"`.

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
