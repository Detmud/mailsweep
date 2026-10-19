# Changelog

Versionsschema: MAJOR.MINOR.PATCH. Die laufende Version steht oben in der Weboberfläche und im Container-Log.

## 1.5.0 (2026-10-19)
- Neu (#3): optionaler Spamfilter mit lokalem Rspamd, standardmäßig aus. Aktivieren mit `COMPOSE_PROFILES=spam` und `SPAM_FILTER=true`.
- mailsweep lädt für Mails der letzten 30 Tage die ersten 512 KB per `BODY.PEEK` (kein Gelesen-Flag), lässt sie von Rspamd bewerten und speichert nur Score und Regelnamen. Pro Sync höchstens 300 Mails, der Rest folgt beim nächsten Sync.
- Rspamd läuft im eigenen Container in einem Docker-Netz ohne Internetzugang. DNS-Blocklisten, Fuzzy-Hashes, Listen von maps.rspamd.com und alle Module mit externen Abfragen sind abgeschaltet, DNS geht ins Leere. Im Test: 0 DNS-Abfragen.
- Neue Spalte „Spam“ (höchster Score je Absender), Filter „nur Spam-Verdacht“, Sortierung nach Spam-Score, Score in den Details.
- Sammelaktion „Spam-Verdacht löschen“: alle Mails ab der Schwelle in den Papierkorb, mit Vorschau. Ausgeblendete Absender bleiben unangetastet.
- Ist Rspamd nicht erreichbar, läuft der Sync trotzdem durch, oben erscheint ein Hinweis.
- Einstellungen: Spamfilter an/aus, Schwelle, Zeitraum, Bewertungen pro Sync.
- Schema 4: neue Spalten werden ergänzt, der Header-Cache bleibt erhalten.
- Fix: Im Detailbereich stand bei Absendern ohne Abmeldelink „null“ vor den Buttons.

## 1.4.0 (2026-10-18)
- Fix (#1): Die DKIM-Spalte wertet jetzt alle `Authentication-Results`-Header des eigenen Mailservers aus, nicht nur den obersten. iCloud setzt pro Prüfung einen eigenen Header (bimi, dmarc, dkim-verifier, spf). Stand der DKIM-Header nicht oben, zeigte die Spalte „?“.
- Vertrauen nach authserv-id: Gewertet werden nur Header, deren Absender in `TRUSTED_AUTHSERV_IDS` steht (Standard `icloud.com`, Suffix-Vergleich). Gefälschte Header mit fremder authserv-id zählen nicht. Ohne passenden Header gilt wie bisher der oberste.
- Neu im Detailbereich eines Absenders: „Authentifizierung (neueste Mail)“ lädt die Authentifizierungs-Header live vom Server und zeigt, welche gewertet werden.
- Der Header-Cache wird nach dem Update einmal neu aufgebaut (Schema 3). Der erste Sync dauert deshalb etwas länger. Regeln, Verlauf und Einstellungen bleiben erhalten.
- Neue Parser-Tests (`tests/test_parse.py`).

## 1.3.1 (2026-10-18)
- Fix (#7): Ordner, die aus „Analysierte Ordner“ entfernt werden oder auf dem Server verschwinden, verschwinden jetzt auch aus Absenderliste, Zählern und Ordnerfilter. Das passiert sofort beim Speichern, nicht erst beim nächsten Sync. Verlauf, Regeln und der Tab „Erledigt“ bleiben erhalten.
- Ändert sich die Ordnerliste während eines laufenden Syncs, liest mailsweep neu hinzugekommene Ordner noch im selben Lauf ein.
- Tooltip in den Einstellungen nennt die Env-Datei statt `mailsweep.env`.

## 1.3.0 (2026-10-18)
- Abmelden endet nach einem Fehler nicht mehr in einer Sackgasse (#4). Der Dialog bietet je nach Lage: erneut über Ausweich-DNS, per Mail abmelden, Link selbst öffnen, als abgemeldet markieren, Archivieren + Regel, Löschen + Regel.
- Blockt ein DNS-Filter (Pi-hole, AdGuard, NextDNS) die Abmelde-Domain (Antwort 0.0.0.0, ::, 127.x), sagt mailsweep das jetzt klar, statt „interne Adresse“ zu melden.
- Neue Einstellung „Ausweich-DNS für Abmelden“ (Standard 9.9.9.9, leer = aus, Env `UNSUB_FALLBACK_DNS`). Wird nur auf Klick und nur für den Abmelde-Host genutzt.
- Sicherheit: Der One-Click-POST geht an genau die IP, die vorher geprüft wurde. Das TLS-Zertifikat wird gegen den echten Hostnamen geprüft. Damit kann kein zweites DNS-Ergebnis (DNS-Rebinding) die Prüfung umgehen. Proxy-Variablen aus der Umgebung werden für Abmelde-Requests ignoriert.
- Testmodus prüft die DNS-Auflösung des Abmelde-Hosts schon vor dem Scharfschalten.
- Bei abgelehnter Signatur Hinweis „Spam nicht abmelden, sondern löschen“.
- Neue Netzwerk-Tests (`tests/test_net.py`): Sinkhole-Erkennung, Ausweich-DNS mit CNAME-Kette, IP-Pinning mit TLS-Prüfung.

## 1.2.0 (2026-10-18)
- Tab „Einstellungen“: Testmodus, analysierte Ordner, Sync-Intervall, Mail-Abmeldung, Archiv-/Papierkorb-Ordner und Abrufgröße direkt in der App änderbar. Werte aus der App überschreiben `mailsweep.env` und lassen sich einzeln zurücksetzen.
- Testmodus per Klick ein- und ausschalten, ohne Neu-Deploy. Beim Ausschalten werden Testeinträge entfernt.
- Versionsnummer in Kopfzeile, Status-API, Container-Label und Log.
- Sync-Intervall greift ohne Neustart.

## 1.1.0 (2026-10-18)
- Spalte „Status“: abgemeldet, X im Papierkorb, X archiviert pro Absender.
- Tab „Erledigt“ mit Summen, Regeln und Resten („Rest löschen“).
- DKIM-Anzeige liest zusätzlich `ARC-Authentication-Results` (iCloud).
- Fix: Im Testmodus wurden Absender fälschlich als abgemeldet markiert.

## 1.0.0 (2026-10-18)
- Erste Version: IMAP-Header-Sync, Absender-/Domain-Ansicht, Abmelden per RFC 8058 und mailto mit DKIM-Prüfung, Archivieren/Papierkorb, Regeln, Protokoll, DRY_RUN.
