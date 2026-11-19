# Változásnapló

## 0.1.0

Az első kiadás.

- Telepítés, frissítés, visszalépés és visszaállítás GitHub-release-ekből Docker-konténerként,
  SHA-256 ellenőrzéssel, a compose-fájl változásainak áttekintésével és állapot-ellenőrzéssel.
- `whiptail` menü magyar szövegekkel; csak olvasó parancsok: `status`, `plan`, `updates`.
- Interaktív `.env.production` varázsló, titkosított jelszótár (scrypt, AES-256, HMAC),
  titkok forgatása.
- Postgres: adatbázis és felhasználó létrehozása (SCRAM-SHA-256), mentés minden frissítés előtt,
  visszatöltés, az egy évnél régebbi mentések törlése.
- Önfrissítés a `telepito.pyz` cseréjével.
