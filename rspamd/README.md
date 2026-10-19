# Rspamd für mailsweep

Optionaler Spamfilter (#3), Anlernen ab 1.6.0 (#12). Aktivieren in der Env-Datei:

```
COMPOSE_PROFILES=spam
SPAM_FILTER=true
RSPAMD_PASSWORD=...   # nur fürs Anlernen; Buchstaben und Ziffern, z. B. openssl rand -hex 24
```

## Was hier anders ist als bei einem normalen Rspamd

- **Kein Internet:** Der Container hängt nur im Docker-Netz `spam` (`internal: true`). Zusätzlich gehen DNS-Abfragen an 127.0.0.1 und scheitern sofort.
- **Keine externen Listen und Dienste:** Abgeschaltet sind DNS-Blocklisten (rbl, surbl, emails), Fuzzy-Hashes (fuzzy_check), ASN, DCC, Razor, Pyzor, SPF, DKIM, DMARC, ARC, BIMI, Whitelists und multimap mit Listen von maps.rspamd.com. mime_types, mid und phishing nutzen nur die mitgelieferten Dateien.
- **Redis nur für Bayes (ab 1.6.0):** Der Container `mailsweep-redis` hat kein Netz und ist nur über einen Unix-Socket im Volume `redis-run` erreichbar (siehe `redis/README.md`). Nur der Bayes-Klassifikator nutzt ihn. Neural, Reputation, Greylisting und Ratelimit bleiben aus.
- **Anlernen nur auf Wunsch:** `autolearn` ist aus. Gelernt wird nur, was mailsweep schickt: „Spam melden“ und „Spam-Verdacht löschen“ als Spam, Mails ausgeblendeter Absender als Ham. Bayes wirkt erst ab 200 angelernten Mails je Klasse (`min_learns`), bis dahin ändert sich an den Scores nichts.
- **Keine SMTP-Daten:** mailsweep liest Mails per IMAP, es gibt keine Absender-IP und kein HELO. `hfilter` ist deshalb aus, `DATE_IN_PAST` zählt 0 Punkte, weil ältere Postfach-Mails sonst immer treffen.
- **Nur intern:** Port 11333 zum Bewerten. Der Controller auf Port 11334 läuft nur, wenn `RSPAMD_PASSWORD` gesetzt ist, und verlangt dann dieses Passwort für alles. Ohne Passwort ist er aus, denn das Ubuntu-Paket bringt sonst das Standardpasswort `q1` mit. Kein Port nach außen.

Was bleibt, sind die lokalen Inhalts- und Header-Regeln: fehlende Message-ID oder Date, verschleierte Betreffzeilen, nur HTML, Links auf IP-Adressen, Linktext zeigt auf eine andere Domain als das Ziel (Phishing), undisclosed recipients und Ähnliches. Das erkennt plumpen Spam und Phishing gut, ist aber schwächer als ein Rspamd mit Blocklisten. Mit genug angelernten Mails kommt Bayes dazu.

iCloud hat SPF, DKIM und DMARC beim Empfang schon geprüft. mailsweep zeigt das Ergebnis in der DKIM-Spalte.

## Lokal prüfen

```bash
docker compose --profile spam build rspamd redis
docker compose --profile spam up -d
docker exec mailsweep python -c "import httpx; print(httpx.get('http://rspamd:11333/ping').text)"
```
