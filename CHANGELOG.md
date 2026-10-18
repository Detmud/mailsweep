# Changelog

Versionsschema: MAJOR.MINOR.PATCH. Die laufende Version steht oben in der Weboberfläche und im Container-Log.

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
