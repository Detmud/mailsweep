# mailsweep: lokales Abmelde-Dashboard für iCloud/IMAP
FROM python:3.12-slim

ARG VERSION=1.7.0
LABEL org.opencontainers.image.title="mailsweep" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Lokales Abmelde- und Aufräum-Dashboard für iCloud/IMAP"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin mailsweep \
 && mkdir -p /data && chown mailsweep:mailsweep /data

COPY app ./app

USER mailsweep
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header"]
