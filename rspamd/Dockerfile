# Rspamd für mailsweep (#3): lokal gebaut aus dem Ubuntu-Paket, nur mit lokalen Regeln.
# Der Container hängt in einem Docker-Netz ohne Internetzugang (compose.yaml, Netz "spam").
FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rspamd \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/rspamd /var/lib/rspamd /run/redis \
 && chown _rspamd:_rspamd /run/rspamd /var/lib/rspamd \
 && chmod 1777 /run/redis

COPY local.d/ /etc/rspamd/local.d/
COPY override.d/ /etc/rspamd/override.d/
RUN rspamadm configtest

USER _rspamd
EXPOSE 11333 11334
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/11333 && printf "GET /ping HTTP/1.0\r\n\r\n" >&3 && grep -q pong <&3'
CMD ["rspamd", "-f"]
