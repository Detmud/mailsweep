# mailsweep

Lokales Abmelde- und Aufräum-Dashboard für dein iCloud-Postfach, à la Leave Me Alone, aber komplett auf deinem Homelab.

- liest per IMAP nur **Header-Metadaten** (Absender, Betreff, Datum, Größe, List-Unsubscribe, Authentication-Results) in eine lokale SQLite; Mail-Inhalte werden nie gespeichert
- gruppiert nach Absender oder Domain, sortiert nach Menge, Speicher oder Aktivität
- **Abmelden** nach RFC 8058 (One-Click-POST) oder per `mailto:`, aber nur, wenn die DKIM-Signatur der neuesten Mail gültig ist, zur Absenderdomain passt und den `List-Unsubscribe`-Header abdeckt. Sonst bekommst du nur den Link angezeigt.
- **Archivieren / Papierkorb** pro Absender, Domain oder als Sammelaktion. Nichts wird endgültig gelöscht: Papierkorb heißt iCloud-Ordner „Deleted Messages“ (30 Tage wiederherstellbar).
- **Regeln** („künftig archivieren/löschen“) laufen nach jedem Sync über die analysierten Ordner und fangen Nachzügler ab.
- Protokoll aller Aktionen, `DRY_RUN`-Modus zum gefahrlosen Ausprobieren

Netzwerkverkehr nach außen: IMAP/SMTP zu Apple, DNS-Abfragen für DKIM-Schlüssel und die Abmelde-Requests selbst. Sonst nichts, keine CDNs, keine Telemetrie.

## Installation auf dem Komodo-Host

### 1. App-spezifisches Passwort bei Apple anlegen

account.apple.com → Anmelden und Sicherheit → App-spezifische Passwörter → „mailsweep“. Das Passwort lässt sich dort jederzeit widerrufen und gibt keinen Zugriff auf deinen Apple Account.

### 2. Dateien auf die Komodo-VM kopieren

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
```

(Alternativ per Upload in code-server nach `/etc/komodo/stacks/` und dort im Terminal entpacken.)

### 3. Zugangsdaten eintragen

```bash
cd /etc/komodo/stacks/mailsweep
sudo cp -p mailsweep.env.example .env
sudo chmod 600 .env
sudo nano .env              # IMAP_USER, IMAP_PASSWORD, UI_PASSWORD setzen
```

`IMAP_USER` ist deine iCloud-Mail-Hauptadresse (bei alten MobileMe-Accounts ggf. `…@me.com`). Schlägt der Login fehl, den Teil vor dem `@` probieren.

Tipp für den ersten Lauf: `DRY_RUN=true`. Dann siehst du alles, aber nichts wird verschoben oder abgemeldet. Wenn die Liste plausibel aussieht, auf `false` stellen und neu deployen.

### 4. Stack in Komodo anlegen

- Stacks → New Stack → Name `mailsweep`, Server `komodo-local`
- Modus **Files on Server**, Run Directory `/etc/komodo/stacks/mailsweep`
- **Auto Update / Poll for Updates aus**: Das Image wird lokal gebaut, es gibt keine Registry.
- Deploy. Komodo baut das Image beim ersten Deploy (ca. 1 Minute).

Ohne Komodo geht es auch direkt: `sudo docker compose up -d --build` im Ordner.

Updates des Codes: Dateien ersetzen, in Komodo „Redeploy“. `pull_policy: build` sorgt dafür, dass neu gebaut wird.

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
| `TRUSTED_AUTHSERV_IDS` | `icloud.com` | Mailserver, deren `Authentication-Results` für die DKIM-Spalte zählen (Suffix-Vergleich) |
| `FETCH_BATCH` | `250` | Header pro IMAP-Abruf; bei Timeouts verkleinern |
| `UI_USER` / `UI_PASSWORD` | `admin` / leer | Basic-Auth der Weboberfläche |

Die Datenbank liegt im Volume `mailsweep-data`. Löschen des Volumes setzt alles zurück (Regeln, Ausblendungen, Protokoll); dein Postfach bleibt davon unberührt.

## Fehlersuche

- **„IMAP-Login fehlgeschlagen“**: App-spezifisches Passwort prüfen, Benutzername mit/ohne Domain probieren.
- **„Server kann weder MOVE noch UID EXPUNGE“**: Der Server unterstützt kein sicheres Verschieben. mailsweep bricht dann ab, statt fremde gelöschte Mails mit zu entfernen.
- **Container startet nicht, Permission denied**: `apparmor:unconfined` ist im Compose bereits gesetzt; sonst Logs in Komodo ansehen.
- Logs: Komodo → Stack `mailsweep` → Log, oder `docker logs mailsweep`.

## Tests

`tests/` enthält einen End-to-End-Test gegen einen echten IMAP-Server (Dovecot). `seed.py` befüllt ein Testpostfach mit DKIM-signierten Newslettern, gefälschten Absendern, kaputten Headern und einem Ordner mit Umlaut. `test_e2e.py` prüft Sync, Gruppierung, DKIM-Prüfung beim Abmelden, die Alternativen bei gesperrtem DNS, Verschieben, Regeln, den Tab „Erledigt“ und den Testmodus.

```bash
python tests/test_net.py   # ohne Mailserver: DNS-Sperren, Ausweich-DNS, IP-Pinning mit TLS
python tests/test_parse.py # ohne Mailserver: DKIM-Ergebnis aus Authentication-Results
python tests/seed.py 127.0.0.1 10143 test@icloud.test geheim /tmp/key.json
python tests/test_e2e.py /tmp      # erwartet key.json im angegebenen Ordner
```

Der Test braucht einen Dovecot mit Klartext-Login auf Port 10143 und den Ordnern `Archive` (`\Archive`) und `Deleted Messages` (`\Trash`).
