"""Auslieferung der Oberfläche (#18): Asset-URLs mit Version und Cache-Control.

Braucht keinen Mailserver. Aufruf: python tests/test_web.py
"""
import os, re, sys, tempfile

os.environ.update(IMAP_HOST="127.0.0.1", IMAP_PORT="1", IMAP_SSL="false", IMAP_USER="u@example.test",
                  IMAP_PASSWORD="x", DATA_DIR=tempfile.mkdtemp(), SYNC_INTERVAL_MINUTES="0",
                  UI_USER="u", UI_PASSWORD="pw")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402
from app.version import __version__  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

with TestClient(main.app) as c:
    ok(c.get("/").status_code == 401, "Seite nur mit Anmeldung")
    c.auth = ("u", "pw")
    r = c.get("/")
    ok(r.status_code == 200 and r.headers["content-type"].startswith("text/html"), "Seite wird ausgeliefert")
    ok(f'src="/static/app.js?v={__version__}"' in r.text, "app.js mit Version")
    ok(f'href="/static/style.css?v={__version__}"' in r.text, "style.css mit Version")
    ok(not re.search(r'"/static/(app\.js|style\.css)"', r.text), "keine Asset-URL ohne Version")
    ok(r.headers.get("cache-control") == "no-cache", "Seite: Cache-Control no-cache")
    ok("default-src 'self'" in r.headers.get("content-security-policy", ""), "CSP bleibt gesetzt")
    a = c.get(f"/static/app.js?v={__version__}")
    ok(a.status_code == 200 and a.headers.get("cache-control") == "no-cache" and a.headers.get("etag"), "app.js: no-cache mit ETag")
    ok(c.get("/static/app.js", headers={"If-None-Match": a.headers["etag"]}).status_code == 304, "Unverändert: 304 ohne Inhalt")
    ok("cache-control" not in c.get("/healthz").headers, "API-Antworten ohne diese Cache-Regel")

print("WEB-TESTS OK")
