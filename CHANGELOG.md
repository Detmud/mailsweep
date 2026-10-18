# Changelog

Versionsschema: MAJOR.MINOR.PATCH. Die laufende Version steht oben in der Weboberfläche und im Container-Log.

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
