# The release contract and `deploy.json`

This document describes **what the installer expects from a repository it installs**. Whoever
makes a project (for example Investment or Games Center) installable must satisfy it.

## 1. The release assets

Every release of an installable repository carries exactly these **assets**. The source archives
that GitHub adds automatically are never downloaded by the installer.

| Asset | Content |
|---|---|
| `deploy.json` | the repository's descriptor (see below) |
| `docker-compose.deploy.yml` | how to run it; **uses only `image:`, there is no `build:`** |
| `image.tar.gz` | `docker save <image>:<version> \| gzip`; the tag is the version **without the `v`** (tag `v1.2.0` → `1.2.0`) |
| `migrations.txt` | one database migration name per line (if there is a database; otherwise empty) |
| `SHA256SUMS` | the SHA-256 sums of the four files above (`sha256sum` format) |

- The version is SemVer (`vMAJOR.MINOR.PATCH[-prerelease]`). Pre-releases are marked by GitHub's
  `prerelease` flag; the installer never shows a draft release.
- Versions are compared by SemVer 2.0 rules (a pre-release is lower than its final release,
  build metadata is ignored).
- `SHA256SUMS` protects against *corruption*; authenticity comes from the GitHub account.

## 2. Rules for the compose file

The installer does **not edit** `docker-compose.deploy.yml`; it passes values to it through
`shared/compose.env` (`--env-file`). Expected in the compose file:

- `image: <name>:${APP_VERSION}`: the installer sets the version;
- `env_file: .env.production`: in the release directory the installer creates a symlink to
  `shared/.env.production`;
- network: `networks: { db: { external: true, name: "${DB_NETWORK}" } }`, and the service joins the
  `db` network. The installer takes `DB_NETWORK` from the chosen database container;
- port: `127.0.0.1:${PORT}:${PORT}` (bound to loopback only); `PORT` comes from `compose.env`;
- `restart: unless-stopped`; a `healthcheck` is optional (the installer does its own health check).

Example:

```yaml
services:
  app:
    image: investment:${APP_VERSION}
    env_file: .env.production
    ports:
      - "127.0.0.1:${PORT}:${PORT}"
    networks: [db]
    restart: unless-stopped
networks:
  db:
    external: true
    name: "${DB_NETWORK}"
```

### Data volumes (anything the application keeps outside the image)

Persistent files must live in a volume, never in the container's own file system and never in
the release directory (`releases/<version>/` is rotated and deleted). Exactly two forms are
allowed, and **every one must be declared in `deploy.json` under `volumes`** (and every declared
volume must be mounted):

1. **A named volume** of the compose project. Do not set `name:` on it and do not mark it
   `external`; Compose then names it `<projectName>_<volume>`, which is what the installer backs
   up and restores.

   ```yaml
   services:
     app:
       volumes:
         - uploads:/app/uploads
   volumes:
     uploads:
   ```

2. **A bind mount below `${DATA_DIR}`.** The installer sets `DATA_DIR` in `compose.env` to
   `<target>/shared/data`, creates `${DATA_DIR}/<hostPath>` (owned by the installer's user) and
   never rotates or removes it. `APP_UID` and `APP_GID` hold the installer user's ids; running
   the container as `user: "${APP_UID}:${APP_GID}"` keeps the files reachable for that user
   (this exact form does not count as a risky change in the compose review).

   ```yaml
       volumes:
         - ${DATA_DIR}/media:/app/media
   ```

**Not allowed** (the installation stops before anything is changed): any other bind mount
(relative ones such as `./data` would be lost with the release directory), mounting
`docker.sock`, `external` volumes, volumes with a custom `name:`, and volumes the compose file
mounts but `deploy.json` does not declare. Unnamed volumes (`- /cache`) are tolerated but
reported as *not backed up*.

For the backup the image must contain `sh` and `tar` (every Debian/Alpine/busybox based image
does; a distroless image does not). The installer starts a throw-away container of the
application's own image for it, so nothing has to be downloaded.

## 3. `deploy.json` (`schemaVersion: 1`)

```json
{
  "schemaVersion": 1,
  "name": "investment",
  "displayName": "Investment",
  "image": { "name": "investment" },
  "compose": { "file": "docker-compose.deploy.yml", "projectName": "investment" },
  "port": { "default": 2570, "env": "PORT" },
  "health": { "path": "/healthz", "timeoutSeconds": 120, "versionField": "version" },
  "database": {
    "required": true,
    "engine": "postgres",
    "description": "The application stores its data in a Postgres database.",
    "urlEnv": "DATABASE_URL",
    "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
    "defaultDatabase": "investment",
    "defaultUser": "investment"
  },
  "migrations": { "listAsset": "migrations.txt" },
  "env": [
    { "name": "NODE_ENV", "fixed": "production" },
    { "name": "BASE_PATH", "description": "The application's sub-path", "default": "/investment/",
      "validate": "^/([a-z0-9._~-]+/)*$", "group": "basic" },
    { "name": "APIKEY_MASTER_SECRET", "description": "Second factor of the API key encryption.",
      "rotationWarning": "After the change the existing API keys can no longer be viewed.",
      "secret": true, "required": true, "generate": { "bytes": 48 }, "minLength": 32, "group": "basic" },
    { "name": "INTRADAY_COLLECT_ENABLED", "default": "false", "validate": "^(true|false)$", "group": "advanced" }
  ],
  "volumes": [
    { "name": "uploads", "description": "Uploaded files", "mountPath": "/app/uploads" },
    { "name": "media", "kind": "bind", "hostPath": "media", "mountPath": "/app/media", "backup": false }
  ],
  "proxy": {
    "note": "Into the Apache VirtualHost :443 block (the installer does not change it):",
    "snippet": "ProxyPass \"{basePath}\" \"http://127.0.0.1:{port}/\"\nProxyPassReverse \"{basePath}\" \"http://127.0.0.1:{port}/\""
  },
  "postInstall": ["Create the first user: docker compose -p investment exec app npx tsx scripts/create-user.ts <name>"],
  "keep": 3
}
```

The paths, variable names, Apache block and `postInstall` in the example are only illustrations;
the installer hard-codes nothing and reads everything from the downloaded `deploy.json`.

### Fields

- **`name`**: the identifier (`^[a-z][a-z0-9-]{0,40}$`); `compose.projectName` and `image.name`
  have the same shape.
- **`port`**: `default` is the default port (1–65535), `env` the name of the compose variable.
- **`health`**: `path` starts with `/`; the installer polls once a second up to `timeoutSeconds`
  and considers the start successful when the answer is 200 and the JSON field `versionField`
  equals the installed version.
- **`database`**: if `required: false` or missing, the installer does not deal with a database.
  `engine` is `postgres` only for now. The `description` (*why* it is needed) is shown before the
  questions. Placeholders of `urlTemplate`: `{user}`, `{password}` (URL-encoded), `{host}`,
  `{port}`, `{database}`. The `urlEnv` variable is filled in by the installer, so it **must not**
  also be listed in `env[]`.
- **`env[]`** items:
  - `name`, `description`, `default`;
  - `fixed`: a fixed value that is never asked;
  - `required`;
  - `secret`: hidden input, also saved to the vault;
  - `generate: {bytes}`: a random secret can be generated;
  - `validate`: a regular expression for the whole value; `minLength`;
  - `rotationWarning`: a warning shown when the secret is rotated (secret items only);
  - `group`: `basic` or `advanced`; the wizard skips `advanced` items with their defaults unless
    asked to show them.
  Values **must not contain** an apostrophe (`'`), a line break or a NUL character.
- **`migrations.listAsset`**: for the downgrade check (the installed version's migrations must not
  be newer than the requested version's).
- **`volumes[]`** (optional): the application's data volumes, see "Data volumes" above.
  - `name`: `^[a-z][a-z0-9_]{0,31}$`; for a named volume it is the key in the compose file's
    `volumes:`, and it appears in the backup file name;
  - `mountPath`: the absolute path inside the container;
  - `kind`: `named` (default) or `bind`; `hostPath`: for `bind` only, the relative directory
    below `${DATA_DIR}` (plain names separated by `/`, at most four, no `..`);
  - `backup`: default `true`. A volume with `"backup": false` is not archived (and the plan and
    the status screen say so);
  - `description`: shown in the plan.
  Names, `mountPath`s and `hostPath`s must be unique.
- **`keep`**: how many earlier releases to keep (default 3).
- **`proxy.snippet`**: informational only, printed at the end of an installation. `{port}` is
  substituted, and so is `{basePath}` by the value of the `BASE_PATH` variable of the
  installation (so the printed block follows the sub-path the administrator chose; using
  `{basePath}` requires `BASE_PATH` in `env[]`).
- **`postInstall`**: to-do items printed at the end of an installation.

Unknown keys are accepted with a warning (forward compatibility), but a missing required field
(`schemaVersion`, `name`, `image`, `port`, `health`) is an error. A larger `schemaVersion` is
rejected ("update the installer").

## 4. Backups, restores and what is never deleted

Before every update, downgrade or reinstall the installer makes a **backup set**: the database
dump and one compressed archive (`<volume>-<version>-<time>.tar.gz`, mode 600) of every volume
with `backup: true`, all with the same time stamp and the version whose data they hold. The
application is stopped while the volumes are copied, so the copy is consistent; it is started
again if the operation is cancelled. Every archive is read back completely before it counts.

- A failed backup is explained and the operation stops, unless the user explicitly chooses to
  continue without that backup.
- If the new release does not start, the recovery menu can roll back to the previous release
  **and** restore the database and volumes from this set (it always asks first).
- The Backups menu can make a set at any time and restore any part of any set. A restore asks
  for the application name, saves the current state first (`…-pre-restore-…`), and replaces the
  *contents* of the volume.
- **Retention:** files older than a year can be deleted, **except that the newest file of each
  series (the database dumps; each volume) is always kept**, however old it is. The files of
  one set expire together.
- The installer never runs `docker compose down -v`, `docker volume rm` or any prune, and the
  `keep` rotation only removes release directories and images. The only deletion inside a
  volume is emptying it during a restore the user confirmed.

## 5. What the application is expected to do

- Serve `health.path` **without authentication**, exposing only what is needed, for example
  `{"status":"ok","version":"1.2.0"}`.
- Take its version from the `APP_VERSION` environment variable (baked into the image) or from
  its package.
- Run its database migrations at startup (`prisma migrate deploy` or similar); migrations only go
  forward, which is why the downgrade check exists.
