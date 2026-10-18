# Rspamd für mailsweep

Optionaler Spamfilter (#3). Aktivieren in der Env-Datei:

```
COMPOSE_PROFILES=spam
SPAM_FILTER=true
```

## Was hier anders ist als bei einem normalen Rspamd

- **Kein Internet:** Der Container hängt nur im Docker-Netz `spam` (`internal: true`). Zusätzlich gehen DNS-Abfragen an 127.0.0.1 und scheitern sofort.
- **Keine externen Listen und Dienste:** Abgeschaltet sind DNS-Blocklisten (rbl, surbl, emails), Fuzzy-Hashes (fuzzy_check), ASN, DCC, Razor, Pyzor, SPF, DKIM, DMARC, ARC, BIMI, Whitelists und multimap mit Listen von maps.rspamd.com. mime_types, mid und phishing nutzen nur die mitgelieferten Dateien.
- **Kein Redis:** Bayes, Neural, Reputation, Greylisting und Ratelimit brauchen Redis und sind aus. Rspamd meldet deshalb beim Start „cannot load Redis parameters for the classifier“, das ist erwartet.
- **Keine SMTP-Daten:** mailsweep liest Mails per IMAP, es gibt keine Absender-IP und kein HELO. `hfilter` ist deshalb aus, `DATE_IN_PAST` zählt 0 Punkte, weil ältere Postfach-Mails sonst immer treffen.
- **Nur Port 11333 intern**, kein Controller, keine Web-UI, kein Port nach außen.

Was bleibt, sind die lokalen Inhalts- und Header-Regeln: fehlende Message-ID oder Date, verschleierte Betreffzeilen, nur HTML, Links auf IP-Adressen, Linktext zeigt auf eine andere Domain als das Ziel (Phishing), undisclosed recipients und Ähnliches. Das erkennt plumpen Spam und Phishing gut, ist aber schwächer als ein Rspamd mit Blocklisten und Bayes.

iCloud hat SPF, DKIM und DMARC beim Empfang schon geprüft. mailsweep zeigt das Ergebnis in der DKIM-Spalte.

## Lokal prüfen

```bash
docker compose --profile spam build rspamd
docker compose --profile spam up -d
docker exec mailsweep python -c "import httpx; print(httpx.get('http://rspamd:11333/ping').text)"
```
