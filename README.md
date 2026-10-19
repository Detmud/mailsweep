# mailsweep

Lokales Abmelde- und Aufräum-Dashboard für dein iCloud-Postfach, à la Leave Me Alone, aber komplett auf deinem Homelab.

- liest per IMAP nur **Header-Metadaten** (Absender, Betreff, Datum, Größe, List-Unsubscribe, Authentication-Results) in eine lokale SQLite; Mail-Inhalte werden nie gespeichert
- gruppiert nach Absender oder Domain, sortiert nach Menge, Speicher oder Aktivität
- **Abmelden** nach RFC 8058 (One-Click-POST) oder per `mailto:`, aber nur, wenn die DKIM-Signatur der neuesten Mail gültig ist, zur Absenderdomain passt und den `List-Unsubscribe`-Header abdeckt. Sonst bekommst du nur den Link angezeigt.
- **Archivieren / Papierkorb** pro Absender, Domain oder als Sammelaktion. Nichts wird endgültig gelöscht: Papierkorb heißt iCloud-Ordner „Deleted Messages“ (30 Tage wiederherstellbar).
- **Regeln** („künftig archivieren/löschen“) laufen nach jedem Sync über die analysierten Ordner und fangen Nachzügler ab.
- Protokoll aller Aktionen, `DRY_RUN`-Modus zum gefahrlosen Ausprobieren
- optional: **Spamfilter** mit lokalem Rspamd ohne Internetzugang (ab 1.5.0, standardmäßig aus)

Netzwerkverkehr nach außen: IMAP/SMTP zu Apple, DNS-Abfragen für DKIM-Schlüssel und die Abmelde-Requests selbst. Sonst nichts, keine CDNs, keine Telemetrie.

## Installation auf dem Komodo-Host

### 1. App-spezifisches Passwort bei Apple anlegen

account.apple.com → Anmelden und Sicherheit → App-spezifische Passwörter → „mailsweep“. Das Passwort lässt sich dort jederzeit widerrufen und gibt keinen Zugriff auf deinen Apple Account.

### 2. Forgejo-Zugang für Komodo

Komodo klont das Repo selbst. Dafür braucht es einen Forgejo-User mit Lesezugriff:

- Forgejo: User `Komodo` anlegen (aktiv), im Repo `home.arpa/mailsweep` unter Einstellungen → Mitarbeiter mit Recht **Lesen** hinzufügen.
- Als dieser User ein Token mit Scope `read:repository` erzeugen.
- Komodo → Settings → Git Accounts: Domain `192.168.10.15:3300`, **HTTPS aus**, User `Komodo`, Token aus dem Schritt davor. Komodo klont direkt von Forgejo auf demselben Host, an Caddy und dessen interner CA vorbei.

### 3. Stack in Komodo anlegen

- Stacks → New Stack → Name `mailsweep`, Server `komodo-local`
- Source **Git Repo**: Git Account aus Schritt 2, Repo `home.arpa/mailsweep`, Branch `main`, Compose File `compose.yaml`
- **Environment**: Inhalt von `mailsweep.env.example` eintragen und anpassen. Passwörter nicht im Klartext, sondern als Secret-Variablen (Komodo → Settings → Variables, Haken *Secret*) und im Feld als `[[MAILSWEEP_IMAP_PASSWORD]]` bzw. `[[MAILSWEEP_UI_PASSWORD]]` referenzieren. Komodo schreibt das Feld beim Deploy nach `.env`, `compose.yaml` liest genau diese Datei.
- **Poll for Updates / Auto Update aus**: Das Image wird lokal gebaut, es gibt keine Registry. Neue Versionen kommen über den Webhook (Schritt 4).
- Deploy. Komodo baut das Image beim ersten Deploy (ca. 1 Minute).

`IMAP_USER` ist deine iCloud-Mail-Hauptadresse (bei alten MobileMe-Accounts ggf. `…@me.com`). Schlägt der Login fehl, den Teil vor dem `@` probieren.

Tipp für den ersten Lauf: `DRY_RUN=true` oder den Testmodus in der App einschalten. Dann siehst du alles, aber nichts wird verschoben oder abgemeldet.

### 4. Automatisch deployen per Webhook

Ziel: Ein Merge nach `main` landet ohne Handarbeit auf dem Host.

1. **Forgejo darf ins LAN senden:** Im Forgejo-Stack `FORGEJO__webhook__ALLOWED_HOST_LIST=private` setzen und Forgejo neu deployen. Der Standard `external` blockt Ziele wie 192.168.x.x.
2. **Webhook im Repo:** Einstellungen → Webhooks → Forgejo. Ziel-URL steht in Komodo im Stack unter *Webhooks* (Form `http://192.168.10.15:9120/listener/github/stack/<Stack-ID>/deploy`). Inhaltstyp `application/json`, Secret = `KOMODO_WEBHOOK_SECRET` aus der Komodo-Core-Konfiguration, nur Push-Events, Branch-Filter `main`.
3. **Komodo:** Im Stack unter *Webhooks* „Webhook Enabled“ und **„Webhook Force Deploy“** einschalten. Ohne Force Deploy führt Komodo nur „Deploy if changed“ aus und vergleicht dabei die Compose-Datei. Bei mailsweep ändert sich meist nur Code, die Compose-Datei bleibt gleich, und der Webhook bewirkt dann nichts.
4. **Test:** Einen PR mergen. In Komodo erscheint unter Updates ein `DeployStack`, danach steht oben in der App die neue Version.

Wichtig: Ab jetzt heißt jeder Push nach `main` „live“. Deshalb `main` schützen: Repo-Einstellungen → Branches → Schutzregel für `main`: Pushen nur per Pull Request, Merge nur nach Freigabe. Neue Funktionen kommen über eigene Branches und PRs.

Ohne Komodo geht es auch direkt: `git clone`, `cp mailsweep.env.example .env`, `.env` anpassen, `sudo docker compose up -d --build`.

### Notfallweg: Dateien von Hand kopieren

Falls Forgejo oder der Webhook nicht verfügbar sind. Stack in Komodo dann auf Source **Files on Server** mit Run Directory `/etc/komodo/stacks/mailsweep` umstellen.

Vom Mac aus, als User `detmud`:

```bash
scp mailsweep.tar.gz detmud@192.168.10.15:~/
ssh detmud@192.168.10.15
```

Auf der VM:

```bash
sudo tar xzf ~/mailsweep.tar.gz -C /etc/komodo/stacks/
rm ~/mailsweep.tar.gz
# gleiche Besitzrechte wie die anderen Stacks übernehmen
sudo chown -R --reference=/etc/komodo/stacks/glance /etc/komodo/stacks/mailsweep
cd /etc/komodo/stacks/mailsweep
sudo cp -p mailsweep.env.example .env && sudo chmod 600 .env
sudo nano .env              # IMAP_USER, IMAP_PASSWORD, UI_PASSWORD setzen
```

Danach in Komodo „Redeploy“. `pull_policy: build` sorgt dafür, dass neu gebaut wird.

### 5. Caddy

Im Caddy-LXC einen Eintrag ergänzen und den Namen `mailsweep.home.arpa` wie deine anderen Dienste im DNS auflösen lassen:

```
mailsweep.home.arpa {
    tls internal
    reverse_proxy 192.168.10.15:8743
}
```

Direkt erreichbar ist es ohnehin unter `http://192.168.10.15:8743`.

## Bedienung

1. Der erste Sync startet automatisch ein paar Sekunden nach dem Container-Start. Bei 50.000 Mails dauert er einige Minuten, der Fortschritt steht oben rechts. Die Liste füllt sich währenddessen schon.
2. Oben die Absender mit den meisten Mails. Filter „nur mit Abmeldelink“ zeigt die Newsletter.
3. **Abmelden** → mailsweep prüft die Signatur und meldet ab. Danach direkt „Archivieren + Regel“ oder „Löschen + Regel“: verschiebt die vorhandenen Mails und alles, was noch nachkommt.
4. Absender, die du behalten willst: **Ausblenden**. Sie verschwinden aus der Liste und bleiben im Postfach unangetastet.
5. Klick auf einen Namen öffnet die Details: letzte Betreffs, Adressen, Roh-Header.
6. Die Spalte **Status** zeigt pro Absender, was schon passiert ist (abgemeldet, X im Papierkorb, X archiviert). Absender, deren Mails komplett verschoben wurden, verschwinden aus der Liste und stehen im Tab **Erledigt**, zusammen mit Summen, Regeln und dem, was noch im Postfach liegt („Rest löschen“). Im DRY_RUN sind diese Einträge als „Test“ markiert und verschwinden beim Umschalten auf `DRY_RUN=false`.

Badges in der Liste:

| Badge | Bedeutung |
|---|---|
| One-Click | Abmeldung per HTTPS-POST nach RFC 8058, ohne Webseite |
| Mail | Abmeldung per Mail an den Absender |
| Link | nur Webseite, musst du selbst öffnen |
| DKIM pass | laut Authentication-Results des iCloud-Servers gültig signiert. Welche Header gewertet werden, zeigt der Detailbereich unter „Authentifizierung“ |
| DKIM fremd | von einer fremden Domain signiert, typisch für Spoofing: nicht abmelden, löschen |

Klappt das Abmelden nicht, zeigt der Dialog, was stattdessen geht: Link selbst öffnen, per Mail abmelden, als abgemeldet markieren oder direkt archivieren bzw. löschen mit Regel.

**DNS-Filter:** Viele Newsletter-Dienste schicken die Abmeldung über Tracking-Domains (z. B. `r.openai.com` über SendGrid). Steht so eine Domain auf der Blockliste deines DNS-Filters, bekommt mailsweep `0.0.0.0` zurück und lehnt aus Sicherheitsgründen ab. Mit „Erneut über DNS 9.9.9.9“ löst mailsweep nur diesen einen Host über den Ausweich-DNS auf. Die Prüfung auf öffentliche Adressen gilt weiter, und die Verbindung geht an genau die geprüfte IP. Welcher Server das ist, stellst du unter Einstellungen ein.

Echter Spam gehört nicht abgemeldet, sondern gelöscht: Ein Abmeldeklick bestätigt dem Spammer nur, dass deine Adresse aktiv ist. mailsweep verweigert das automatische Abmelden bei ungültiger oder fremder Signatur.

### Spamfilter (optional)

Für Spam ohne Abmeldelink kann mailsweep jede neue Mail von einem lokalen Rspamd bewerten lassen. Einschalten in der Env-Datei (bei Komodo im Feld *Environment*):

```
COMPOSE_PROFILES=spam
SPAM_FILTER=true
```

Danach neu deployen. `COMPOSE_PROFILES=spam` startet den Container `mailsweep-rspamd` mit, `SPAM_FILTER` schaltet die Bewertung ein (auch unter Einstellungen änderbar).

- Bewertet werden Mails der letzten 30 Tage, pro Sync höchstens 300. Dafür lädt mailsweep die ersten 512 KB jeder Mail per IMAP, ohne sie als gelesen zu markieren. Gespeichert werden nur Score und Regelnamen, nie der Inhalt.
- Rspamd hat keinen Internetzugang (Docker-Netz `internal`) und fragt keine Blocklisten oder sonstigen Dienste ab. Details und was das für die Erkennung bedeutet: `rspamd/README.md`.
- In der Liste erscheinen die Spalte **Spam**, der Filter „nur Spam-Verdacht“ und der Knopf **Spam-Verdacht löschen**. Er verschiebt alle Mails ab der Schwelle (Standard 6) in den Papierkorb, nach einer Vorschau. Ausgeblendete Absender bleiben unangetastet.
- Ist Rspamd nicht erreichbar, läuft der Sync trotzdem, oben steht dann ein Hinweis.

## Konfiguration

Die meisten Optionen lassen sich im Tab **Einstellungen** ändern, ohne Neu-Deploy (ab 1.2.0). Werte aus der App überschreiben die Env-Datei. Zugangsdaten und UI-Passwort stehen nur in der Env-Datei `.env` (bei Komodo im Stack-Feld *Environment*, Passwörter als Secret-Variablen).

Alle Optionen stehen kommentiert in `mailsweep.env.example`. Die wichtigsten:

| Variable | Standard | Zweck |
|---|---|---|
| `SCAN_FOLDERS` | `INBOX` | Kommagetrennte Ordner, die analysiert und von Regeln bearbeitet werden |
| `ARCHIVE_FOLDER` / `TRASH_FOLDER` | automatisch | Werden per SPECIAL-USE bzw. iCloud-Namen erkannt |
| `SYNC_INTERVAL_MINUTES` | `30` | Abstand der automatischen Syncs, `0` = nur manuell |
| `DRY_RUN` | `false` | Nur protokollieren, nichts ausführen |
| `ALLOW_MAILTO_UNSUBSCRIBE` | `true` | Abmelde-Mails über `smtp.mail.me.com` senden |
| `UNSUB_FALLBACK_DNS` | `9.9.9.9` | Ausweich-DNS, falls dein DNS-Filter eine Abmelde-Domain blockt; nur auf Klick, leer = aus |
| `SPAM_FILTER` | `false` | Spam-Bewertung mit lokalem Rspamd; braucht zusätzlich `COMPOSE_PROFILES=spam` |
| `SPAM_THRESHOLD` | `6` | Ab diesem Rspamd-Score gilt eine Mail als Spam-Verdacht |
| `SPAM_MAX_AGE_DAYS` / `SPAM_PER_SYNC` / `SPAM_MAX_KB` | `30` / `300` / `512` | Welche Mails bewertet werden und wie viel pro Sync und Mail geladen wird |
| `TRUSTED_AUTHSERV_IDS` | `icloud.com` | Mailserver, deren `Authentication-Results` für die DKIM-Spalte zählen (Suffix-Vergleich) |
| `FETCH_BATCH` | `250` | Header pro IMAP-Abruf; bei Timeouts verkleinern |
| `UI_USER` / `UI_PASSWORD` | `admin` / leer | Basic-Auth der Weboberfläche |

Die Datenbank liegt im Volume `mailsweep-data`. Löschen des Volumes setzt alles zurück (Regeln, Ausblendungen, Protokoll); dein Postfach bleibt davon unberührt.

## Fehlersuche

- **„IMAP-Login fehlgeschlagen“**: App-spezifisches Passwort prüfen, Benutzername mit/ohne Domain probieren.
- **„Server kann weder MOVE noch UID EXPUNGE“**: Der Server unterstützt kein sicheres Verschieben. mailsweep bricht dann ab, statt fremde gelöschte Mails mit zu entfernen.
- **Container startet nicht, Permission denied**: `apparmor:unconfined` ist im Compose bereits gesetzt; sonst Logs in Komodo ansehen.
- **Merge nach `main`, aber keine neue Version**: In Forgejo unter Webhooks → letzte Zustellungen prüfen. Antwort 200, aber in Komodo kein `DeployStack` unter Updates: „Webhook Force Deploy“ fehlt (siehe Installation, Schritt 4). Fehler „Not allowed“ in Forgejo: `ALLOWED_HOST_LIST` fehlt. Antwort 401/403: Secret passt nicht.
- Logs: Komodo → Stack `mailsweep` → Log, oder `docker logs mailsweep`.

## Tests

`tests/` enthält einen End-to-End-Test gegen einen echten IMAP-Server (Dovecot). `seed.py` befüllt ein Testpostfach mit DKIM-signierten Newslettern, gefälschten Absendern, kaputten Headern und einem Ordner mit Umlaut. `test_e2e.py` prüft Sync, Gruppierung, DKIM-Prüfung beim Abmelden, die Alternativen bei gesperrtem DNS, Verschieben, Regeln, den Tab „Erledigt“ und den Testmodus.

```bash
python tests/test_net.py   # ohne Mailserver: DNS-Sperren, Ausweich-DNS, IP-Pinning mit TLS
python tests/test_parse.py # ohne Mailserver: DKIM-Ergebnis aus Authentication-Results
python tests/test_spam.py  # Spamfilter gegen Dovecot mit Fake-Rspamd; RSPAMD_URL=http://127.0.0.1:11333 für einen echten
python tests/seed.py 127.0.0.1 10143 test@icloud.test geheim /tmp/key.json
python tests/test_e2e.py /tmp      # erwartet key.json im angegebenen Ordner
```

Der Test braucht einen Dovecot mit Klartext-Login auf Port 10143 und den Ordnern `Archive` (`\Archive`) und `Deleted Messages` (`\Trash`).
