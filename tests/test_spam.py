"""Spamfilter (#3) gegen einen echten IMAP-Server (Dovecot).

Standard: ein Fake-Rspamd im Test bewertet deterministisch (Mail mit "paypal" = 15, sonst 0.5)
und merkt sich, wie viele Bytes er pro Mail bekommt.
Mit RSPAMD_URL=http://127.0.0.1:11333 läuft der Test gegen einen echten Rspamd mit rspamd/local.d.

Aufruf: python tests/test_spam.py   (Dovecot wie bei test_e2e.py auf 127.0.0.1:10143)
"""
import imaplib, json, os, sys, tempfile, threading, time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer

REAL = os.environ.get("RSPAMD_URL")
FOLDER = "Spamtest"
HOST, PORT, USER, PW = "127.0.0.1", 10143, "test@icloud.test", "geheim"

received: list[bytes] = []


class FakeRspamd(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"pong\r\n")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append(body)
        spam = b"paypal" in body.lower()
        out = {"score": 15.0 if spam else 0.5, "action": "reject" if spam else "no action",
               "symbols": {"PHISHING": {"score": 2.0}, "R_UNDISC_RCPT": {"score": 3.0}} if spam
               else {"MIME_UNKNOWN": {"score": 0.1}, "FROM_HAS_DN": {"score": 0}}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


if not REAL:
    srv = HTTPServer(("127.0.0.1", 0), FakeRspamd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
else:
    url = REAL

os.environ.update(IMAP_HOST=HOST, IMAP_PORT=str(PORT), IMAP_SSL="false", IMAP_USER=USER, IMAP_PASSWORD=PW,
                  SCAN_FOLDERS=FOLDER, DATA_DIR=tempfile.mkdtemp(), SYNC_INTERVAL_MINUTES="0",
                  UI_PASSWORD="pw", UI_USER="u", SPAM_FILTER="true", RSPAMD_URL=url, SPAM_MAX_KB="16",
                  SPAM_THRESHOLD="6", SPAM_MAX_AGE_DAYS="30")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
from app import main, jobs, db  # noqa: E402
from app.config import settings  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))


def mail(frm, to, subject, body, html=False, date=None, mid=True):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    if date is not False:
        m["Date"] = formatdate(date or time.time(), localtime=True)
    if mid:
        m["Message-ID"] = make_msgid(domain="x.example")
    if html:
        m.set_content(body, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


PHISH = ('<html><body><p>Dear customer, your account is suspended!!! Verify now.</p>'
         '<a href="http://198.51.100.7/login.php">https://www.paypal.com/signin</a></body></html>')

# Testordner frisch anlegen und befüllen
s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
s.delete(FOLDER); s.create(FOLDER)
now = time.time()
add = lambda raw, t=now: s.append(FOLDER, None, imaplib.Time2Internaldate(t), raw)
add(mail("Anna <anna@private.example>", USER, "Treffen am Samstag", "Hallo, passt dir Samstag um 15 Uhr? Anna"))
for i in range(3):
    add(mail('"PayPal Service" <service@paypa1-secure.example>', "undisclosed-recipients:;",
             "URGENT: YOUR ACCOUNT IS SUSPENDED!!!", PHISH, html=True, date=False, mid=False))
# Ausgeblendeter Absender mit Spam: darf nicht gelöscht werden
add(mail('"PayPal Kept" <keep@kept-sender.example>', "undisclosed-recipients:;",
         "URGENT: YOUR ACCOUNT IS SUSPENDED!!!", PHISH, html=True, date=False, mid=False))
# Große Mail (200 KB): darf nur teilweise geladen werden
add(mail("Big <big@private.example>", USER, "Fotos", "Hallo\n" + "x" * 200_000))
# Alte Mail (90 Tage): außerhalb von SPAM_MAX_AGE_DAYS, wird nicht bewertet
old = now - 90 * 86400
add(mail('"PayPal Old" <old@paypa1-secure.example>', "undisclosed-recipients:;", "OLD", PHISH, html=True, date=old), old)
s.logout()

with TestClient(main.app) as c:
    c.auth = ("u", "pw")
    H = {"X-Mailsweep": "1"}
    c.post("/api/action", headers=H, json={"group": "addr", "keys": ["keep@kept-sender.example"], "action": "keep"})
    jobs.run_sync()
    st = c.get("/api/status").json()
    sp = st["spam"]
    ok(not st["sync"]["error"] and not sp["error"], f"Sync mit Spamfilter ohne Fehler ({st['sync']['error']} {sp['error']})")
    ok(st["messages"] == 7, f"7 Mails im Testordner ({st['messages']})")
    ok(sp["enabled"] and sp["scored"] == 6 and sp["pending"] == 0, f"6 bewertet, alte Mail ausgelassen ({sp})")
    ok(sp["suspect"] == 4, f"4 Mails über Schwelle 6 ({sp['suspect']})")

    with db.connect() as con:
        rows = {r["from_addr"]: dict(r) for r in con.execute("SELECT from_addr, spam_score, spam_symbols FROM messages")}
        cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
    ok(rows["service@paypa1-secure.example"]["spam_score"] >= 6, f"Phishing über Schwelle ({rows['service@paypa1-secure.example']})")
    ok(rows["anna@private.example"]["spam_score"] < 6, f"normale Mail unter Schwelle ({rows['anna@private.example']})")
    ok("PHISHING" in rows["service@paypa1-secure.example"]["spam_symbols"], "Regelnamen gespeichert")
    ok(rows["old@paypa1-secure.example"]["spam_score"] is None, "alte Mail nicht bewertet")
    ok(not any(c_ in cols for c_ in ("body", "content", "raw")), "kein Inhalt in der Datenbank")
    if not REAL:
        ok(max(len(b) for b in received) <= 16 * 1024, f"Teilabruf: höchstens 16 KB pro Mail ({max(len(b) for b in received)})")

    # Gelesen-Flag bleibt unberührt
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW); s.select(FOLDER, readonly=True)
    ok(s.uid("SEARCH", None, "SEEN")[1][0] == b"", "BODY.PEEK setzt kein \\Seen")
    s.logout()

    r = c.get("/api/senders", params={"only_spam": "true", "show_kept": "true"}).json()
    keys = {x["key"] for x in r["rows"]}
    ok(keys == {"service@paypa1-secure.example", "keep@kept-sender.example"}, f"Filter nur Spam-Verdacht ({keys})")
    r = c.get("/api/senders", params={"sort": "spam"}).json()
    top = r["rows"][0]
    ok(top["key"] == "service@paypa1-secure.example" and top["spam_hits"] == 3 and top["spam_known"] == 3,
       f"Sortierung nach Spam-Score ({top['key']} {top['spam_hits']})")
    det = c.get("/api/sender", params={"key": "service@paypa1-secure.example"}).json()
    ok(det["messages"][0]["spam_score"] >= 6, "Score in den Details")

    p = c.post("/api/spam/trash", headers=H, json={"preview": True}).json()
    ok(p["count"] == 3 and p["senders"] == 1 and p["min_score"] == 6, f"Vorschau ohne ausgeblendete Absender ({p})")
    t = c.post("/api/spam/trash", headers=H, json={"min_score": 6}).json()
    ok(t["moved"] == 3 and t["target"] == "Deleted Messages", f"3 Mails in den Papierkorb ({t})")
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
    ok(int(s.select(FOLDER, readonly=True)[1][0]) == 4, "Server: 4 Mails im Testordner übrig")
    s.logout()
    dn = {x["key"]: x for x in c.get("/api/done").json()["rows"]}
    ok(dn["service@paypa1-secure.example"]["moves"]["trash"] == 3, "Tab Erledigt zählt die Spam-Löschung")
    lg = c.get("/api/log").json()["log"]
    ok(any(x["action"] == "spam-trash" and "3 Mails" in x["detail"] for x in lg), "Protokoll-Eintrag")

    # Rspamd nicht erreichbar: Sync läuft weiter, Fehler wird angezeigt
    settings.rspamd_url = "http://127.0.0.1:9"
    with db.connect() as con:
        con.execute("UPDATE messages SET spam_score=NULL")
    jobs.run_sync()
    st = c.get("/api/status").json()
    ok(not st["sync"]["error"] and "nicht erreichbar" in st["spam"]["error"], f"Rspamd aus: Sync ok, Hinweis ({st['spam']['error'][:60]})")
    ok(not c.get("/api/spam/check").json()["ok"], "Prüf-Endpoint meldet Fehler")
    settings.rspamd_url = url
    jobs.run_sync()
    ok(c.get("/api/status").json()["spam"]["error"] == "", "Fehler verschwindet, sobald Rspamd wieder antwortet")

    # Ausschalten: keine Bewertung, Endpoint verweigert
    r = c.post("/api/settings", headers=H, json={"values": {"spam_filter": False}})
    ok(r.status_code == 200 and c.post("/api/spam/trash", headers=H, json={"preview": True}).status_code == 400,
       "Spamfilter aus: Sammelaktion gesperrt")

# Migration: Schema-3-Datenbank bekommt die Spalten, ohne den Cache zu verwerfen
import sqlite3  # noqa: E402
d = tempfile.mkdtemp(); path = os.path.join(d, "mailsweep.sqlite3")
con = sqlite3.connect(path)
con.executescript(db.SCHEMA.replace("    spam_score      REAL,                   -- NULL = noch nicht bewertet\n", "")
                  .replace("    spam_symbols    TEXT    NOT NULL DEFAULT '',\n", "")
                  .replace("    spam_learned    TEXT    NOT NULL DEFAULT '',  -- 'spam' | 'ham' | 'skip' (#12)\n", ""))
con.execute("INSERT INTO meta VALUES ('schema','3')")
con.execute("INSERT INTO messages(folder, uid, from_addr) VALUES ('INBOX', 1, 'a@b.example')"); con.commit(); con.close()
settings.data_dir = d
db.init_db()
with db.connect() as con:
    cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    v = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
ok("spam_score" in cols and "spam_symbols" in cols and n == 1 and v == str(db.SCHEMA_VERSION),
   f"Migration 3 -> {db.SCHEMA_VERSION} behält Cache ({n}, {v})")
print("SPAM-TESTS OK" + (" (echter Rspamd)" if REAL else ""))
