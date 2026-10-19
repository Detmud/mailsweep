#!/usr/bin/env bash
# Alle Tests in einem Rutsch, für den Forgejo-Runner (#11) und lokal.
#
# Braucht: root, python3 (>= 3.12) mit venv, dovecot-imapd, openssl, optional rspamd und redis-server.
# Startet einen eigenen Dovecot auf 127.0.0.1:10143 mit frischem Postfach in einem
# Temp-Verzeichnis. Die Ports 10143, 11333 und 11334 müssen dafür frei sein.
# Mit rspamd und redis-server im PATH laufen test_spam.py, test_learn.py und test_mails.py zusätzlich gegen
# einen echten Rspamd mit der Konfiguration aus rspamd/ und Redis per Unix-Socket, ohne
# /etc/rspamd anzufassen.
#
# Aufruf: bash tests/ci.sh     (aus dem Repo-Wurzelverzeichnis oder von überall)
#   CI_SKIP_RSPAMD=1           echten Rspamd auslassen
#   CI_VENV=/pfad              vorhandenes venv benutzen statt ein neues anzulegen
set -euo pipefail

cd "$(dirname "$0")/.."
WORK=$(mktemp -d)
PIDS=()
cleanup() {
  local rc=$? p f
  if [ "$rc" != 0 ]; then  # bei Fehlern die Dienst-Logs zeigen
    for f in "$WORK/dc/dovecot.log" "$WORK/rspamd/rspamd.log" "$WORK/redis/redis.log"; do
      [ -f "$f" ] && { echo "--- $f"; tail -30 "$f"; }
    done
  fi
  [ -f "$WORK/dc/run/master.pid" ] && PIDS+=("$(cat "$WORK/dc/run/master.pid")")
  [ -f "$WORK/redis/redis.pid" ] && PIDS+=("$(cat "$WORK/redis/redis.pid")")
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  for _ in $(seq 1 20); do  # bis zu 10 s warten, bis Dovecot und Rspamd weg sind
    local alive=0
    for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" = 0 ] && break
    sleep 0.5
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$*"; }

port_open() {
  python3 - "$1" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

wait_port() {  # wait_port PORT NAME
  for _ in $(seq 1 60); do port_open "$1" && return 0; sleep 0.5; done
  echo "$2 auf Port $1 nicht erreichbar" >&2
  return 1
}

# ---------------------------------------------------------------- Python
step "Python-Umgebung"
if [ -n "${CI_VENV:-}" ]; then
  PY="$CI_VENV/bin/python"
else
  python3 -m venv "$WORK/venv"
  PY="$WORK/venv/bin/python"
  "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt cryptography ruff
fi
"$PY" --version

# ---------------------------------------------------------------- Lint
step "ruff (Fehler, keine Stilfragen)"
"$PY" -m ruff check --select F,E9 app tests

# ---------------------------------------------------------------- ohne Mailserver
step "test_parse.py"
"$PY" tests/test_parse.py
step "test_net.py"
"$PY" tests/test_net.py
step "test_web.py"
"$PY" tests/test_web.py

# ---------------------------------------------------------------- Dovecot
for p in 10143 11333 11334; do
  if port_open $p; then echo "Port $p ist schon belegt, bitte den laufenden Test-Dienst stoppen." >&2; exit 1; fi
done

step "Dovecot starten"
id vmail >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin vmail
mkdir -p "$WORK/dc/run" "$WORK/dc/state" "$WORK/mail"
chown vmail:vmail "$WORK/mail"
chmod 755 "$WORK"
echo 'test@icloud.test:{PLAIN}geheim' > "$WORK/dc/users"
sed -e "s|@WORK@|$WORK|g" tests/dovecot.conf > "$WORK/dc/dovecot.conf"
/usr/sbin/dovecot -c "$WORK/dc/dovecot.conf"
wait_port 10143 Dovecot

step "seed.py"
"$PY" tests/seed.py 127.0.0.1 10143 test@icloud.test geheim "$WORK/key.json"

step "test_e2e.py"
"$PY" tests/test_e2e.py "$WORK"

step "test_spam.py (Fake-Rspamd)"
"$PY" tests/test_spam.py

step "test_learn.py (Fake-Rspamd)"
"$PY" tests/test_learn.py

step "test_mails.py (Fake-Rspamd)"
"$PY" tests/test_mails.py

# ---------------------------------------------------------------- echter Rspamd + Redis
if [ -z "${CI_SKIP_RSPAMD:-}" ] && command -v rspamd >/dev/null && command -v redis-server >/dev/null; then
  step "Redis und Rspamd mit rspamd/ starten"
  RS="$WORK/rspamd"
  RD="$WORK/redis"
  mkdir -p "$RS/conf" "$RS/db" "$RS/run" "$RD"
  RDUSER=redis; id "$RDUSER" >/dev/null 2>&1 || RDUSER=root
  chown "$RDUSER" "$RD"
  # wie im Container: kein TCP, nur Unix-Socket
  su -s /bin/sh "$RDUSER" -c "redis-server --port 0 --unixsocket $RD/redis.sock --unixsocketperm 777 \
    --dir $RD --save '' --daemonize yes --pidfile $RD/redis.pid --logfile $RD/redis.log"
  for _ in $(seq 1 20); do [ -S "$RD/redis.sock" ] && break; sleep 0.5; done
  cp -r rspamd/local.d rspamd/override.d "$RS/conf/"
  sed -i "s|/run/redis/redis.sock|$RD/redis.sock|" "$RS/conf/local.d/classifier-bayes.conf"
  RUSER=_rspamd; id "$RUSER" >/dev/null 2>&1 || RUSER=rspamd
  chown -R "$RUSER" "$RS/db" "$RS/run"
  VARS=("--var=LOCAL_CONFDIR=$RS/conf" "--var=DBDIR=$RS/db" "--var=RUNDIR=$RS/run")
  export RSPAMD_CONTROLLER_PASSWORD
  RSPAMD_CONTROLLER_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))")
  rspamadm "${VARS[@]}" configtest
  rspamd -f -u "$RUSER" -g "$RUSER" "${VARS[@]}" > "$RS/rspamd.log" 2>&1 &
  PIDS+=($!)
  wait_port 11333 Rspamd || exit 1
  wait_port 11334 Rspamd-Controller || exit 1
  step "test_spam.py (echter Rspamd)"
  RSPAMD_URL=http://127.0.0.1:11333 "$PY" tests/test_spam.py
  step "test_learn.py (echter Rspamd mit Redis)"
  RSPAMD_URL=http://127.0.0.1:11333 RSPAMD_CONTROLLER_URL=http://127.0.0.1:11334 \
    RSPAMD_PASSWORD="$RSPAMD_CONTROLLER_PASSWORD" "$PY" tests/test_learn.py
  step "test_mails.py (echter Rspamd mit Redis)"
  RSPAMD_URL=http://127.0.0.1:11333 RSPAMD_CONTROLLER_URL=http://127.0.0.1:11334 \
    RSPAMD_PASSWORD="$RSPAMD_CONTROLLER_PASSWORD" "$PY" tests/test_mails.py
else
  echo "Rspamd oder Redis nicht installiert oder ausgelassen, nur Fake-Rspamd getestet."
fi

step "ALLES GRÜN"
