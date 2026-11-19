# Telepítő (`telepito`)

Interaktív, a szerveren kézzel indított eszköz, amely egy **privát GitHub-repó verziózott
release-ét** letölti, és Docker-konténerként **telepíti vagy frissíti**. A build a GitHubon
történik; a szerverre csak az a néhány fájl kerül le, ami a futtatáshoz kell (nincs forráskód,
nincs build). A telepítő egyetlen fájl (`telepito.pyz`), csak a Python szabványos könyvtárát és
a szerveren amúgy is meglévő eszközöket (`whiptail`, `openssl`, `docker`) használja.

A felületi szövegek magyarul vannak; a kód, a kommentek és a commit-üzenetek angolul.

## Amit tud

- **Telepítés és frissítés** egy kiválasztott release-ből: letöltés, SHA-256 ellenőrzés,
  a compose-fájl változásainak áttekintése, Docker-kép betöltése, indítás, állapot-ellenőrzés,
  és csak siker után átváltás az új kiadásra (`current` link).
- **Visszalépés** korábbi verzióra, az adatbázis-migrációk ellenőrzésével és figyelmeztetéssel.
- **Hiba esetén megerősítéssel** visszaállítás az előző kiadásra (soha nem automatikusan).
- **Elérhető frissítések** listája (indításkor is megnézi).
- **A `.env.production` varázsló**, titkok generálása, a titkosított **jelszótár**, titkok forgatása.
- **Adatbázis** a megosztott Postgresben: létrehozás (SCRAM), mentés minden frissítés előtt,
  visszatöltés, az egy évnél régebbi mentések törlése.
- **Önfrissítés** (`telepito.pyz` cseréje ellenőrzéssel).

Amit nem csinál: operációs rendszer frissítése, Apache vagy systemd módosítása (csak kiírja a
javasolt beállítást), root-jog használata (ha root kell, kiírja a pontos parancsot), több gépre
telepítés.

## Követelmények a szerveren

| | |
|---|---|
| Rendszer | Ubuntu 22.04, x86_64 |
| Python | `python3` 3.10 vagy újabb (külső csomag nem kell) |
| Eszközök | `whiptail`, `openssl`, `curl`, `jq`, `sha256sum` |
| Docker | Docker Engine és Docker Compose v2 (`docker compose`) |
| Felhasználó | a `docker` csoport tagja; **sudo nem kell**, root-ként a telepítő nem indul |

## Első beállítás a szerveren

Az 1. lépés root jogot igényel, ezt a telepítő soha nem futtatja helyetted.

### 1. 🔧 Mappák (root)

```bash
sudo mkdir -p /var/www/telepito && sudo chown deploy:webapps /var/www/telepito && sudo chmod 2775 /var/www/telepito
sudo mkdir -p /var/www/<alkalmazás> && sudo chown deploy:webapps /var/www/<alkalmazás> && sudo chmod 2775 /var/www/<alkalmazás>
```

(A `deploy:webapps` helyett a saját felhasználód és csoportod is jó; a lényeg, hogy az a
felhasználó írhassa, aki a telepítőt futtatja.)

### 2. GitHub-token létrehozása

GitHub → *Settings → Developer settings → Fine-grained personal access tokens*:

- **Repository access:** csak a szükséges repók: ez a repó (az önfrissítéshez) és a telepítendő
  projektek repói;
- **Permissions:** *Contents: Read*, *Metadata: Read*;
- **Expiration:** legfeljebb 90 nap.

### 3. A telepítő első letöltése

A token a `curl` konfigurációján át, **stdin-en** megy, így a folyamatlistában sem látszik.

```bash
(
  set -euo pipefail
  cd /var/www/telepito
  read -rsp 'GitHub-token: ' TOKEN; echo
  REPO=bbalazs2002/Installer
  api() { printf 'header = "Authorization: Bearer %s"
' "$TOKEN" | curl -fsSL -K - "$@"; }

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

A parancsok egy alshellben futnak (`set -e`), ezért bármelyik hiba (rossz token, nincs release,
nem egyező összeg) megállítja a folyamatot, és a meglévő `telepito.pyz` érintetlen marad. A
sikeres futás jele a `telepito.pyz: OK` sor; a token csak az alshell változójában él.

### 4. Indítás és a token megadása

```bash
/var/www/telepito/telepito.pyz
```

(Vagy `python3 /var/www/telepito/telepito.pyz`. A kényelemért: `ln -s /var/www/telepito/telepito.pyz ~/bin/telepito`.
Az önfrissítés a szimbolikus linket követi, és a valódi fájlt cseréli.)

*Beállítások → GitHub-token megadása és ellenőrzése*: a token rejtett bevitellel kerül a
`~/.config/telepito/token` fájlba (600-as jogosultság). A telepítő megnézi, hogy a token
hozzáfér-e egy repóhoz, és figyelmeztet, ha 14 napon belül lejár.

Ezután *Telepítés / frissítés*: repó, kiadás, célmappa, port, terv, megerősítés.

## Használat

```
Telepítő — főmenü
 Telepítés / frissítés
 Elérhető frissítések
 Jelszavak és .env
 Visszaállítás előző verzióra
 Adatbázis (mentés, visszatöltés, mentések törlése)
 Állapot
 Naplók
 Beállítások (GitHub-token, ismert repók)
 A telepítő frissítése
 Kilépés
```

Minden képernyőn az **Esc** vagy a **Vissza** egy lépést visz vissza. A lapozható szövegdobozban
(napló, terv, különbség) a **Tab** az *OK* gombra ugrik, onnan az Enter bezárja.

A menü mellett csak olvasó, diagnosztikai parancsok vannak (módosítani csak a menü tud, nincs
`--yes` kapcsoló):

| Parancs | Mit csinál |
|---|---|
| `telepito --version` | a verzió kiírása |
| `telepito status [--json]` | a telepített alkalmazások állapota (titkot nem ír ki) |
| `telepito plan <owner/repo> <verzió> <célmappa>` | a terv kiírása; semmit nem módosít |
| `telepito updates` | az elérhető frissítések listája |

Kilépési kódok: `0` siker, `1` hiba, `2` a felhasználó megszakította, `3` hiányzó előfeltétel.

## Mi hol van a szerveren

```
/var/www/telepito/                 2775
  telepito.pyz                     a telepítő (önfrissíthető), mellette telepito.pyz.previous
  repos.json                       ismert repók
  installs.json                    nyilvántartás (titok nélkül)
  vault/vault.enc                  titkosított jelszótár (600)
  logs/YYYY-MM-DD.log              napló (a titkok kimaszkolva)
  tmp/                             ideiglenes letöltések (a telepítő törli)
  .lock                            egyidejű futás ellen
~/.config/telepito/token           GitHub-token (600)

/var/www/<alkalmazás>/
  releases/<verzió>/               a kiadás (compose, deploy.json, .env.production -> ../../shared/...)
  current -> releases/<verzió>     atomi átváltás
  shared/.env.production           600  az alkalmazás futásidejű környezete (titkokkal)
  shared/compose.env               600  a compose változói (APP_VERSION, PORT, DB_NETWORK)
  backups/<db>-<verzió>-<idő>.dump 600  adatbázis-mentések
  .install.json                    telepítési nyilvántartás (jelszó nélkül)
```

## Biztonsági elvek

- **Nincs belépés a szerverre kívülről.** A GitHub semmit nem telepít: a szerver csak *olvas* a
  GitHubból, egy csak olvasásra szóló tokennel, kézi indítással.
- **Integritás:** minden letöltött fájlt a `SHA256SUMS` alapján ellenőriz. Ez a sérülés ellen
  véd; a hitelességet a GitHub-fiók adja. Ezért frissítéskor a compose-fájl változása
  **megjelenik**, a veszélyes beállítások (`privileged`, új `volumes`, nem loopbackre kötött
  `ports`, `docker.sock`, ...) külön megerősítést kérnek, a `build:` pedig tilos.
- **Titkok:** a token csak az `Authorization` fejlécben utazik (átirányításnál a fejlécet
  eldobja), a jelszavak soha nem kerülnek parancssori argumentumba vagy környezeti változóba,
  a napló és a képernyő minden ismert titkot `***`-ra cserél. A jelszótár scrypt + AES-256 +
  HMAC (encrypt-then-MAC); a mester-jelszót sehol nem tárolja. **Ha elfelejted, a jelszótár
  nem állítható helyre.**
- **Nincs root:** a telepítő sosem hív `sudo`-t, és root-ként el sem indul.
- **Visszaállítás csak megerősítéssel**, adatbázis-visszatöltés csak az adatbázis nevének
  begépelésével, és előbb mindig mentés készül a jelenlegi állapotról.

## Egy projekt telepíthetővé tétele

A telepítendő repó minden release-ében pontosan meghatározott mellékleteknek kell lenniük
(`deploy.json`, `docker-compose.deploy.yml`, `image.tar.gz`, `migrations.txt`, `SHA256SUMS`).
A részleteket, a `deploy.json` mezőit és egy példát a [docs/release-contract.md](docs/release-contract.md)
írja le.

## Fejlesztés

Csak a Python szabványos könyvtára kell a futáshoz; a fejlesztéshez `ruff` és `mypy`.

```bash
python -m pip install ruff mypy
python -m unittest discover -s tests -t .      # az összes teszt
ruff check . && ruff format --check . && mypy  # statikus ellenőrzés
python scripts/build_pyz.py                    # dist/telepito.pyz
```

- A tesztek hamis GitHubbal (helyi `http.server`), hamis Dockerrel és hamis felülettel futnak.
- A **valódi Docker tesztek** (`postgres:17-alpine` és `postgres:15-alpine` eldobható
  konténerek, `docker compose config`) és a **valódi `whiptail` tesztek** (Linux, pty) külön
  jelöltek; Docker nélkül vagy `TELEPITO_SKIP_DOCKER_TESTS=1` mellett kimaradnak.
- Windowson a Linux-specifikus részek (`flock`, `whiptail`, atomi link-csere) tartalék úton
  futnak; a teljes ellenőrzés Linuxon történik (CI: Python 3.10 és 3.12).

## Új kiadás készítése

1. A `src/telepito/__init__.py`-ban a `__version__` átírása, a `CHANGELOG.md` frissítése, commit.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. A `release.yml` lefuttatja a teljes CI-t, ellenőrzi, hogy a címke egyezik a verzióval,
   felépíti a `telepito.pyz`-t, és létrehozza a release-t két melléklettel:
   `telepito.pyz` és `SHA256SUMS`. A `-rc.1` stb. végű címkék előzetes kiadások, az önfrissítés
   ezeket nem kínálja fel.
