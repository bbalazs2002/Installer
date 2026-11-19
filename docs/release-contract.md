# A release szerződése és a `deploy.json`

Ez a dokumentum azt írja le, **mit vár a telepítő egy telepítendő repótól**. Aki egy projektet
(például az Investmentet vagy a Games Centert) telepíthetővé tesz, ezt kell teljesítenie.

## 1. A release mellékletei

Egy telepíthető repó minden release-ében pontosan ezek a **mellékletek** (assets) vannak. A
GitHub által automatikusan hozzáadott forrás-archívumokat a telepítő soha nem tölti le.

| Melléklet | Tartalom |
|---|---|
| `deploy.json` | a repó leírója (lásd lent) |
| `docker-compose.deploy.yml` | a futtatás leírása; **csak `image:`-t használ, `build:` nincs** |
| `image.tar.gz` | `docker save <image>:<verzió> \| gzip`; a címke a verzió **`v` nélkül** (a `v1.2.0` címkéhez `1.2.0`) |
| `migrations.txt` | soronként egy adatbázis-migráció neve (ha van adatbázis; különben üres) |
| `SHA256SUMS` | a fenti négy fájl SHA-256 összege (`sha256sum` formátum) |

- A verzió SemVer (`vMAJOR.MINOR.PATCH[-előzetes]`). Az előzetes kiadásokat a GitHub
  `prerelease` jelölése adja; piszkozat (`draft`) release-t a telepítő soha nem mutat.
- Az összehasonlításnál a SemVer 2.0 szabályait követi (az előzetes verzió kisebb a hozzá tartozó
  véglegesnél, a build-metaadat nem számít).
- A `SHA256SUMS` a *sérülés* ellen véd, a hitelességet a GitHub-fiók adja.

## 2. A compose-fájlra vonatkozó szabályok

A telepítő a `docker-compose.deploy.yml`-t **nem szerkeszti**; értékeket ad át neki a
`shared/compose.env` fájlon (`--env-file`) keresztül. A compose-ban elvárt elemek:

- `image: <név>:${APP_VERSION}`: a telepítő állítja be a verziót;
- `env_file: .env.production`: a release mappában a telepítő egy szimbolikus linket hoz létre a
  `shared/.env.production` fájlra;
- hálózat: `networks: { db: { external: true, name: "${DB_NETWORK}" } }`, a szolgáltatás a `db`
  hálózatra csatlakozik. A `DB_NETWORK` értékét a telepítő a kiválasztott adatbázis-konténerből veszi;
- port: `127.0.0.1:${PORT}:${PORT}` (kizárólag loopbackre kötve); a `PORT` a `compose.env`-ből jön;
- `restart: unless-stopped`; a `healthcheck` nem kötelező (a telepítő maga végez állapot-ellenőrzést).

Minta:

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

## 3. A `deploy.json` (`schemaVersion: 1`)

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
    "description": "Az alkalmazás Postgres adatbázisban tárolja az adatait.",
    "urlEnv": "DATABASE_URL",
    "urlTemplate": "postgresql://{user}:{password}@{host}:{port}/{database}",
    "defaultDatabase": "investment",
    "defaultUser": "investment"
  },
  "migrations": { "listAsset": "migrations.txt" },
  "env": [
    { "name": "NODE_ENV", "fixed": "production" },
    { "name": "BASE_PATH", "description": "Az alkalmazás alútvonala", "default": "/investment/",
      "validate": "^/([a-z0-9._~-]+/)*$", "group": "basic" },
    { "name": "APIKEY_MASTER_SECRET", "description": "Az API-kulcsok titkosításának második tényezője.",
      "rotationWarning": "A csere után a meglévő API-kulcsok nem lesznek megtekinthetők.",
      "secret": true, "required": true, "generate": { "bytes": 48 }, "minLength": 32, "group": "basic" },
    { "name": "INTRADAY_COLLECT_ENABLED", "default": "false", "validate": "^(true|false)$", "group": "advanced" }
  ],
  "proxy": {
    "note": "Az Apache VirtualHost :443 blokkjába (a telepítő nem módosítja):",
    "snippet": "ProxyPass \"/investment/\" \"http://127.0.0.1:{port}/\"\nProxyPassReverse \"/investment/\" \"http://127.0.0.1:{port}/\""
  },
  "postInstall": ["Az első felhasználó létrehozása: docker compose -p investment exec app npx tsx scripts/create-user.ts <név>"],
  "keep": 3
}
```

A példában szereplő útvonalak, változónevek, az Apache-blokk és a `postInstall` csak
illusztrációk; a telepítő semmit nem éget be, mindent a letöltött `deploy.json`-ból olvas.

### Mezők

- **`name`**: az azonosító (`^[a-z][a-z0-9-]{0,40}$`); a `compose.projectName` és az `image.name`
  ugyanilyen alakú.
- **`port`**: `default` az alapértelmezett port (1–65535), `env` a compose változó neve.
- **`health`**: a `path` `/`-jellel kezdődik; a telepítő másodpercenként kérdezi a
  `timeoutSeconds`-ig, és akkor tekinti sikeresnek, ha a válasz 200, és a JSON `versionField`
  mezője egyezik a telepített verzióval.
- **`database`**: ha `required: false` vagy hiányzik, a telepítő nem foglalkozik adatbázissal.
  Az `engine` egyelőre csak `postgres`. A `description` (hogy *miért* kell) megjelenik a
  kérdezés előtt. A `urlTemplate` helyőrzői: `{user}`, `{password}` (URL-kódolva), `{host}`,
  `{port}`, `{database}`. A `urlEnv` változót a telepítő tölti ki, ezért **nem szerepelhet** az
  `env[]` listában.
- **`env[]`** elemei:
  - `name`, `description`, `default`;
  - `fixed`: rögzített érték, nem kérdezi;
  - `required`: kötelező;
  - `secret`: rejtett bevitel, a jelszótárba is kerül;
  - `generate: {bytes}`: véletlen titok generálható;
  - `validate`: reguláris kifejezés a teljes értékre; `minLength`;
  - `rotationWarning`: figyelmeztetés a titok cseréjekor (csak `secret` elemnél);
  - `group`: `basic` vagy `advanced`; az `advanced` elemeket a varázsló alapból kihagyja az
    alapértékkel, kérésre mutatja.
  Az értékek **nem tartalmazhatnak** aposztrófot (`'`), sortörést vagy NUL karaktert.
- **`migrations.listAsset`**: a visszalépés-ellenőrzéshez (a telepített verzió migrációi
  nem lehetnek újabbak, mint a kért verzióé).
- **`keep`**: hány korábbi kiadást őrizzen meg (alap: 3).
- **`proxy.snippet`**: csak tájékoztató kiírás a telepítés végén (`{port}` behelyettesítődik).
- **`postInstall`**: a telepítés végén kiírt teendők.

Ismeretlen kulcsot a telepítő figyelmeztetéssel átenged (előre-kompatibilitás), de a hiányzó
kötelező mezőt (`schemaVersion`, `name`, `image`, `port`, `health`) hibaként elutasítja. A
`schemaVersion` nagyobb értékét is elutasítja („frissítsd a telepítőt”).

## 4. Az alkalmazás elvárt viselkedése

- A `health.path` címen **hitelesítés nélkül** elérhető, és csak a szükséges adatot adja ki,
  például `{"status":"ok","version":"1.2.0"}`.
- A verziót az `APP_VERSION` környezeti változóból (a képbe építve) vagy a csomagból veszi.
- Az adatbázis-migrációkat induláskor maga futtatja (`prisma migrate deploy` vagy hasonló);
  a migrációk csak előre mennek, ezért van a visszalépés-ellenőrzés.
