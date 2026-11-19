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
  "proxy": {
    "note": "Into the Apache VirtualHost :443 block (the installer does not change it):",
    "snippet": "ProxyPass \"/investment/\" \"http://127.0.0.1:{port}/\"\nProxyPassReverse \"/investment/\" \"http://127.0.0.1:{port}/\""
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
- **`keep`**: how many earlier releases to keep (default 3).
- **`proxy.snippet`**: informational only, printed at the end of an installation (`{port}` is
  substituted).
- **`postInstall`**: to-do items printed at the end of an installation.

Unknown keys are accepted with a warning (forward compatibility), but a missing required field
(`schemaVersion`, `name`, `image`, `port`, `health`) is an error. A larger `schemaVersion` is
rejected ("update the installer").

## 4. What the application is expected to do

- Serve `health.path` **without authentication**, exposing only what is needed, for example
  `{"status":"ok","version":"1.2.0"}`.
- Take its version from the `APP_VERSION` environment variable (baked into the image) or from
  its package.
- Run its database migrations at startup (`prisma migrate deploy` or similar); migrations only go
  forward, which is why the downgrade check exists.
