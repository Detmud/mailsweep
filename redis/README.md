# Redis für Rspamd-Bayes (#12)

Nur nötig, wenn mailsweep Spam anlernen soll. Startet mit `COMPOSE_PROFILES=spam` zusammen mit Rspamd.

- Gebaut aus dem Ubuntu-24.04-Paket `redis-server`, wie Rspamd. Keine fremden Images.
- `network_mode: none`: Der Container hat gar kein Netz. Rspamd erreicht ihn nur über den Unix-Socket `/run/redis/redis.sock` im gemeinsamen Volume `redis-run`. mailsweep hängt dieses Volume nicht ein und kommt an Redis nicht heran.
- Daten im Volume `redis-data` (`dump.rdb`, alle 5 Minuten bei Änderungen). Darin stehen nur Bayes-Statistiken: Hashes von Wörtern mit Zählern für Spam und Ham, kein Mailtext.
- Zurücksetzen der Anlern-Daten: Stack stoppen, Volume `mailsweep_redis-data` löschen, neu deployen. Die Zähler in mailsweep laufen weiter, sie zeigen, wie viele Mails mailsweep insgesamt zum Anlernen geschickt hat.
