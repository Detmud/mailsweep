# Redis für die Bayes-Daten von Rspamd (#12), lokal gebaut aus dem Ubuntu-Paket.
# Läuft ohne Netzwerk (network_mode: none) und spricht nur über einen Unix-Socket im
# gemeinsamen Volume /run/redis. mailsweep selbst hat keinen Zugriff darauf.
FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends redis-server \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/redis /var/lib/redis \
 && chown redis:redis /var/lib/redis \
 && chmod 1777 /run/redis

USER redis
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD redis-cli -s /run/redis/redis.sock ping | grep -q PONG
# Kein TCP-Port. Socket für alle im Volume beschreibbar: nur Rspamd hängt es zusätzlich ein.
# noeviction: Bayes-Daten nie stillschweigend verwerfen. Snapshot alle 5 Minuten bei Änderungen.
CMD ["redis-server", "--port", "0", \
     "--unixsocket", "/run/redis/redis.sock", "--unixsocketperm", "777", \
     "--dir", "/var/lib/redis", "--save", "300 1", "--appendonly", "no", \
     "--maxmemory", "200mb", "--maxmemory-policy", "noeviction"]
