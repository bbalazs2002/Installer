# Installer (`telepito`)

An interactive tool, started by hand on the server, that downloads a **versioned release of a
private GitHub repository** and **installs or updates** it as a Docker container. The build
happens on GitHub; only the few files needed to run the application reach the server (no source
code, no build). The installer itself is a single file (`telepito.pyz`) that uses only the Python
standard library and tools the server already has (`whiptail`, `openssl`, `docker`).

The program is called `telepito` (its command, its `telepito.pyz` release asset and its
`--version` output) so that installers deployed earlier can keep updating themselves; the
Python package inside is `installer`.

## What it does

- **Install and update** from a chosen release: download, SHA-256 verification, review of the
  compose file changes, loading the Docker image, start, health check, and only after success
  the switch to the new release (the `current` link).
- **Downgrade** to an older version, with a check of the database migrations and a warning.
- **On failure, recovery only with confirmation** (it never rolls back on its own).
- **Available updates** list (also checked when the menu opens).
- **A `.env.production` wizard**, secret generation, an encrypted **vault**, secret rotation.
- **Database** in a shared Postgres: creation (SCRAM), a backup before every update, restore,
  and deletion of backups older than a year.
- **Self-update** (replacing `telepito.pyz` with verification).

What it does not do: update the operating system, change Apache or systemd (it only prints the
suggested configuration), use root privileges (when root is needed it prints the exact command),
install on several machines.

## Requirements on the server

| | |
|---|---|
| System | Ubuntu 22.04, x86_64 |
| Python | `python3` 3.10 or newer (no extra packages) |
| Tools | `whiptail`, `openssl`, `curl`, `jq`, `sha256sum` |
| Docker | Docker Engine and Docker Compose v2 (`docker compose`) |
| User | member of the `docker` group; **no sudo needed**, and the installer refuses to run as root |

## First setup on the server

Step 1 needs root; the installer never runs it for you.

### 1. 🔧 The installer directory (root)

The installer keeps its own files in the directory that contains `telepito.pyz` — you choose
where that is (the examples use `/var/Installer`). Create it for the user who will run the
installer:

```bash
sudo mkdir -p /var/Installer && sudo chown "$USER" /var/Installer
```

(If several people share it, use a shared group and mode `2775`.) Every application is installed
into a directory of its own, for example:

```bash
sudo mkdir -p /var/www/<application> && sudo chown "$USER" /var/www/<application>
```

### 2. Create a GitHub token

GitHub → *Settings → Developer settings → Fine-grained personal access tokens*:

- **Repository access:** only the repositories you need: this one (for self-update) and the
  repositories of the projects you install;
- **Permissions:** *Contents: Read*, *Metadata: Read*;
- **Expiration:** at most 90 days.

### 3. Download the installer for the first time

The token reaches `curl` through its configuration on **stdin**, so it never shows up in the
process list. The commands run in a subshell with `set -e`: any failure (wrong token, no release,
checksum mismatch) stops them, and an existing `telepito.pyz` stays untouched.

```bash
(
  set -euo pipefail
  cd /var/Installer
  read -rsp 'GitHub token: ' TOKEN; echo
  REPO=bbalazs2002/Installer
  api() { printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -fsSL -K - "$@"; }

  rm -rf .download && mkdir .download && cd .download
  release=$(api -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest")
  for name in telepito.pyz SHA256SUMS; do
    id=$(printf '%s' "$release" | jq -r --arg n "$name" '.assets[] | select(.name == $n) | .id')
    api -H 'Accept: application/octet-stream' -o "$name" "https://api.github.com/repos/$REPO/releases/assets/$id"
  done

  sha256sum --check SHA256SUMS
  chmod 750 telepito.pyz
  mv telepito.pyz ../telepito.pyz
  cd .. && rm -rf .download
)
```

A successful run prints `telepito.pyz: OK`. The token only lives in the subshell's variable.

### 4. Start it and enter the token

```bash
/var/Installer/telepito.pyz
```

(Or `python3 /var/Installer/telepito.pyz`. For convenience:
`ln -s /var/Installer/telepito.pyz ~/bin/telepito` — the installer follows the symlink to the real
file, so its own files and the self-update always use the real directory.)

Open *Settings → Set and verify the GitHub token*: the token is entered hidden and saved to
`~/.config/installer/token` (mode 600). The installer checks that it can reach a repository and
warns when the token expires within 14 days.

Then *Install / update*: repository, release, target directory, port, plan, confirmation.

To keep the installer's files somewhere else than next to `telepito.pyz`, use `--state-dir
<dir>` or the `TELEPITO_STATE_DIR` environment variable; the token file can be moved with
`--token-file <file>` or `TELEPITO_TOKEN_FILE`. A token saved by 0.1.0 in
`~/.config/telepito/token` is still read when `~/.config/installer/token` does not exist.

## Usage

```
Installer — main menu
 Install / update
 Available updates
 Passwords and .env
 Roll back to a previous version
 Database (backup, restore, delete backups)
 Status
 Logs
 Settings (GitHub token, known repositories)
 Update the installer
 Quit
```

On every screen **Esc** or **Back** goes one step back. In the scrollable text boxes (log, plan,
diff) press **Tab** to jump to the *OK* button, then Enter closes the box.

Next to the menu there are read-only diagnostic commands (only the menu can change anything;
there is no `--yes` switch):

| Command | What it does |
|---|---|
| `telepito --version` | print the version |
| `telepito status [--json]` | the state of the installed applications (prints no secrets) |
| `telepito plan <owner/repo> <version> <target>` | print the plan; changes nothing |
| `telepito updates` | list the available updates |

Exit codes: `0` success, `1` failure, `2` cancelled by the user, `3` a prerequisite is missing.

## What lives where on the server

```
<installer directory>/             the directory that contains telepito.pyz
  telepito.pyz                     the installer (self-updating); next to it telepito.pyz.previous
  repos.json                       known repositories
  installs.json                    the registry of installations (no secrets)
  vault/vault.enc                  encrypted vault (600)
  logs/YYYY-MM-DD.log              log (secrets masked)
  tmp/                             temporary downloads (the installer cleans them up)
  .lock                            prevents concurrent runs
~/.config/installer/token           GitHub token (600)

/var/www/<application>/            (the target directory you choose)
  releases/<version>/              the release (compose, deploy.json, .env.production -> ../../shared/...)
  current -> releases/<version>    atomic switch
  shared/.env.production           600  the application's runtime environment (with secrets)
  shared/compose.env               600  compose variables (APP_VERSION, PORT, DB_NETWORK)
  backups/<db>-<version>-<time>.dump  600  database backups
  .install.json                    installation record (no passwords)
```

## Security principles

- **No way into the server from outside.** GitHub installs nothing: the server only *reads* from
  GitHub, with a read-only token, started by hand.
- **Integrity:** every downloaded file is verified against `SHA256SUMS`. That protects against
  corruption; authenticity comes from the GitHub account. That is why the compose file changes
  are **shown** on every update, dangerous settings (`privileged`, new `volumes`, ports not bound
  to loopback, `docker.sock`, ...) need a separate confirmation, and `build:` is forbidden.
- **Secrets:** the token travels only in the `Authorization` header (the header is dropped on a
  redirect), passwords never appear in command-line arguments or environment variables, and the
  log and the screen replace every known secret with `***`. The vault uses scrypt + AES-256 +
  HMAC (encrypt-then-MAC); the master password is stored nowhere. **If you forget it, the vault
  cannot be recovered.**
- **No root:** the installer never calls `sudo`, and refuses to start as root.
- **Recovery only with confirmation**; a database restore needs the database name typed in, and a
  backup of the current state is always made first.

## Making a project installable

Every release of an installable repository must carry exactly the assets defined in the contract
(`deploy.json`, `docker-compose.deploy.yml`, `image.tar.gz`, `migrations.txt`, `SHA256SUMS`). The
details, the fields of `deploy.json` and an example are in
[docs/release-contract.md](docs/release-contract.md).

## Development

Running needs only the Python standard library; development needs `ruff` and `mypy`.

```bash
python -m pip install ruff mypy
python -m unittest discover -s tests -t .      # all tests
ruff check . && ruff format --check . && mypy  # static checks
python scripts/build_pyz.py                    # dist/telepito.pyz
```

- Tests run against a fake GitHub (a local `http.server`), a fake Docker and a fake UI.
- The **real Docker tests** (throw-away `postgres:17-alpine` and `postgres:15-alpine` containers,
  `docker compose config`) and the **real `whiptail` tests** (Linux, pty) are marked separately;
  they are skipped without Docker or with `TELEPITO_SKIP_DOCKER_TESTS=1`.
- On Windows the Linux-specific parts (`flock`, `whiptail`, atomic link replacement) use fallbacks;
  the full verification runs on Linux (CI: Python 3.10 and 3.12).

## Making a new release

1. Change `__version__` in `src/installer/__init__.py`, update `CHANGELOG.md`, commit.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `release.yml` runs the whole CI, checks that the tag matches the version, builds
   `telepito.pyz` and creates the release with two assets: `telepito.pyz` and `SHA256SUMS`.
   Tags ending in `-rc.1` and the like are pre-releases; the self-update does not offer them.

Keep the program name (`telepito`), the `telepito.pyz` asset name and the `telepito <version>`
output of `--version` unchanged: installers already deployed rely on them to update themselves.
