"""Anlernen (Bayes) und Score-Regel (#12) gegen einen echten IMAP-Server (Dovecot).

Standard: ein Fake-Rspamd spielt Scanner und Controller zugleich. Er bewertet deterministisch
(Mail mit "paypal" = 15, sonst 0.5), prüft beim Anlernen das Passwort und merkt sich, was er lernt.
Mit RSPAMD_URL, RSPAMD_CONTROLLER_URL und RSPAMD_PASSWORD läuft der Test gegen einen echten Rspamd
mit Redis (siehe tests/ci.sh).

Aufruf: python tests/test_learn.py   (Dovecot wie bei test_e2e.py auf 127.0.0.1:10143)
"""
import imaplib, json, os, sys, tempfile, threading, time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer

REAL = os.environ.get("RSPAMD_URL")
FOLDER = "Lerntest"
HOST, PORT, USER, PW = "127.0.0.1", 10143, "test@icloud.test", "geheim"
SECRET = os.environ.get("RSPAMD_PASSWORD", "lernpasswort")

learned: list[tuple[str, bytes]] = []
denied: list[str] = []


class FakeRspamd(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"pong\r\n")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path in ("/learnspam", "/learnham"):
            if self.headers.get("Password") != SECRET:
                denied.append(self.path)
                return self._send(403, {"error": "Unauthorized"})
            kind = self.path[6:]
            if (kind, body) in learned:
                return self._send(208, {"error": f"has been already learned as {kind}, ignore it"})
            learned.append((kind, body))
            return self._send(200, {"success": True})
        spam = b"paypal" in body.lower()
        self._send(200, {"score": 15.0 if spam else 0.5, "action": "no action",
                         "symbols": {"PHISHING": {"score": 2.0}} if spam else {}})


if not REAL:
    srv = HTTPServer(("127.0.0.1", 0), FakeRspamd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = ctl = f"http://127.0.0.1:{srv.server_port}"
else:
    url, ctl = REAL, os.environ.get("RSPAMD_CONTROLLER_URL", "http://127.0.0.1:11334")

os.environ.update(IMAP_HOST=HOST, IMAP_PORT=str(PORT), IMAP_SSL="false", IMAP_USER=USER, IMAP_PASSWORD=PW,
                  SCAN_FOLDERS=FOLDER, DATA_DIR=tempfile.mkdtemp(), SYNC_INTERVAL_MINUTES="0",
                  UI_PASSWORD="pw", UI_USER="u", SPAM_FILTER="true", RSPAMD_URL=url, SPAM_MAX_KB="64",
                  RSPAMD_CONTROLLER_URL=ctl, RSPAMD_PASSWORD=SECRET, SPAM_THRESHOLD="6", SPAM_LEARN_MAX="10")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
from app import main, jobs, db  # noqa: E402
from app.config import settings  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
n_learn = lambda kind: sum(1 for k, _ in learned if k == kind)

WORDS = ("garden river window coffee mountain letter yellow summer pencil travel music orange "
         "basket candle forest planet silver winter bridge market").split()


def mail(frm, subject, body, html=False, n=0, phish=False):
    m = EmailMessage()
    # Phishing wie in test_spam.py: ohne Empfänger, Datum und Message-ID, damit auch ein echter
    # Rspamd sicher über die Schwelle kommt
    m["From"], m["To"], m["Subject"] = frm, "undisclosed-recipients:;" if phish else USER, subject
    if not phish:
        m["Date"] = formatdate(time.time() - n * 60, localtime=True)
        m["Message-ID"] = make_msgid(domain="x.example")
    # Genug unterschiedliche Wörter, damit Bayes die Mail nicht als zu kurz überspringt
    text = body + "\n\n" + " ".join(WORDS[(n + i) % len(WORDS)] for i in range(40)) + f" nummer {n}"
    if html:
        m.set_content(f"<html><body><p>{text}</p></body></html>", subtype="html")
    else:
        m.set_content(text)
    return m.as_bytes()


PHISH = ('Dear customer, your account is suspended!!! Verify now. '
         '<a href="http://198.51.100.7/login.php">https://www.paypal.com/signin</a>')


def fill(mails):
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
    for raw in mails:
        s.append(FOLDER, None, imaplib.Time2Internaldate(time.time()), raw)
    s.logout()


def server_count():
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
    n = int(s.select(FOLDER, readonly=True)[1][0]); s.logout()
    return n


s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
s.delete(FOLDER); s.create(FOLDER); s.logout()
fill([mail("Anna <anna@private.example>", "Treffen am Samstag", "Hallo, passt dir Samstag um 15 Uhr?", n=1)]
     + [mail("Shop <news@kept.example>", f"Newsletter {i}", "Unsere Angebote der Woche", n=10 + i) for i in range(3)]
     + [mail("Junk <offer@junk.example>", f"Angebot {i}", "Sie haben gewonnen, jetzt Gewinn abholen", n=20 + i) for i in range(2)]
     + [mail('"PayPal Service" <service@paypa1-secure.example>', "URGENT: YOUR ACCOUNT IS SUSPENDED!!!", PHISH, html=True, n=30 + i, phish=True) for i in range(3)])

with TestClient(main.app) as c:
    c.auth = ("u", "pw")
    H = {"X-Mailsweep": "1"}
    act = lambda keys, action: c.post("/api/action", headers=H, json={"group": "addr", "keys": keys, "action": action}).json()

    # 1. Ausgeblendeter Absender wird beim Sync als Ham angelernt
    act(["news@kept.example"], "keep")
    jobs.run_sync()
    st = c.get("/api/status").json(); ln = st["spam"]["learn"]
    ok(not st["sync"]["error"] and not st["spam"]["error"], f"Sync ohne Fehler ({st['sync']['error']} {st['spam']['error']})")
    ok(ln["enabled"] and ln["configured"] and ln["ham"] == 3 and not ln["error"], f"3 Mails als Ham angelernt ({ln})")
    if not REAL:
        ok(n_learn("ham") == 3 and all(b"news@kept.example" in b for k, b in learned if k == "ham"), "Fake-Controller bekam genau die 3")
    jobs.run_sync()
    ln = c.get("/api/status").json()["spam"]["learn"]
    ok(ln["ham"] == 3 and (REAL or n_learn("ham") == 3), f"Zweiter Sync lernt nichts doppelt ({ln['ham']})")
    with db.connect() as con:
        marks = {r[0]: r[1] for r in con.execute("SELECT from_addr, GROUP_CONCAT(DISTINCT spam_learned) FROM messages GROUP BY from_addr")}
    ok(marks["news@kept.example"] == "ham" and marks["anna@private.example"] == "", f"Markierung in der DB ({marks})")

    # 2. Spam melden: erst anlernen, dann Papierkorb
    r = act(["offer@junk.example"], "spam")
    ok(r["moved"] == 2 and r["learned"] == 2 and r["target"] == "Deleted Messages" and not r["learn_error"], f"Spam melden: 2 angelernt, 2 verschoben ({r})")
    ok(server_count() == 7, "Server: 7 Mails übrig")
    ln = c.get("/api/status").json()["spam"]["learn"]
    ok(ln["spam"] == 2, f"Zähler Spam ({ln['spam']})")
    dn = {x["key"]: x for x in c.get("/api/done").json()["rows"]}
    ok(dn["offer@junk.example"]["moves"]["trash"] == 2, "Tab Erledigt zählt Spam melden als Papierkorb")

    # 3. Spam-Verdacht löschen lernt ebenfalls an
    t = c.post("/api/spam/trash", headers=H, json={"min_score": 6}).json()
    ok(t["moved"] == 3 and t["learned"] == 3, f"Sammelaktion: 3 angelernt, 3 verschoben ({t})")
    lg = c.get("/api/log").json()["log"]
    ok(any(x["action"] == "spam-learn" and "als Spam angelernt" in x["detail"] for x in lg), "Protokoll-Eintrag fürs Anlernen")

    # 4. Falsches Passwort: Verschieben klappt trotzdem, Fehler wird angezeigt
    fill([mail("Junk2 <x@junk2.example>", "Noch ein Angebot", "Gratis Gewinn wartet", n=40)])
    jobs.run_sync()
    settings.rspamd_password = "falsch"
    r = act(["x@junk2.example"], "spam")
    ok(r["moved"] == 1 and r["learned"] == 0 and "Passwort" in r["learn_error"], f"Falsches Passwort: verschoben, Fehler gemeldet ({r})")
    ok("Passwort" in c.get("/api/status").json()["spam"]["learn"]["error"], "Fehler im Status")
    settings.rspamd_password = SECRET

    # 5. Testmodus: nichts anlernen, nichts verschieben
    fill([mail("Junk3 <y@junk3.example>", "Angebot drei", "Gratis Gewinn wartet hier", n=50)])
    jobs.run_sync()
    before = len(learned)
    c.post("/api/settings", headers=H, json={"values": {"dry_run": True}})
    r = act(["y@junk3.example"], "spam")
    ok(r["dry_run"] and r["learned"] == 0 and not r["learn_error"] and server_count() == 5 and len(learned) == before,
       f"Testmodus: weder angelernt noch verschoben, kein alter Fehler ({r})")
    c.post("/api/settings", headers=H, json={"values": {"dry_run": False}})

    # 6. Ohne Passwort: Anlernen aus, Spam melden verschiebt nur
    settings.rspamd_password = ""
    st = c.get("/api/status").json()["spam"]["learn"]
    ok(not st["enabled"] and not st["configured"], "Ohne RSPAMD_PASSWORD kein Anlernen")
    r = act(["y@junk3.example"], "spam")
    ok(r["moved"] == 1 and r["learned"] == 0 and not r["learn_error"] and len(learned) == before, f"Ohne Passwort nur verschoben ({r})")
    settings.rspamd_password = SECRET

    # 7. Score-Regel: nach dem Sync automatisch in den Papierkorb, ausgeblendete Absender ausgenommen
    fill([mail('"PayPal Again" <again@paypa1-secure.example>', "URGENT: YOUR ACCOUNT IS SUSPENDED!!!", PHISH, html=True, n=60 + i, phish=True) for i in range(2)]
         + [mail('"PayPal Kept" <news@kept.example>', "URGENT: YOUR ACCOUNT IS SUSPENDED!!!", PHISH, html=True, n=70, phish=True)])
    ok(c.post("/api/settings", headers=H, json={"values": {"spam_auto_score": 3}}).status_code == 400, "Auto-Schwelle unter 6 abgelehnt")
    c.post("/api/settings", headers=H, json={"values": {"dry_run": True, "spam_auto_trash": True, "spam_auto_score": 6}})
    jobs.run_sync()
    ok(server_count() == 7, "Score-Regel im Testmodus: nichts verschoben")
    c.post("/api/settings", headers=H, json={"values": {"dry_run": False}})
    jobs.run_sync()
    with db.connect() as con:
        left = sorted(r[0] for r in con.execute("SELECT from_addr FROM messages"))
    ok(server_count() == 5 and "again@paypa1-secure.example" not in left and left.count("news@kept.example") == 4,
       f"Score-Regel: 2 gelöscht, ausgeblendeter Absender bleibt ({left})")
    st = c.get("/api/status").json()["spam"]
    ok(st["auto"] == {"enabled": True, "score": 6}, f"Status zeigt die Regel ({st['auto']})")
    lg = c.get("/api/log").json()["log"]
    ok(any(x["action"] == "spam-rule" and "2 Mails" in x["detail"] for x in lg), "Protokoll-Eintrag der Regel")
    ok(st["learn"]["spam"] == 5, f"Regel lernt nicht automatisch an ({st['learn']['spam']})")

    # 8. Einstellungen zeigen das Passwort nie
    raw = c.get("/api/settings").text + c.get("/api/status").text
    ok(SECRET not in raw, "Passwort taucht in keiner Antwort auf")

    if not REAL:
        ok(denied == ["/learnspam"], f"Fake-Controller hat nur den Versuch mit falschem Passwort abgelehnt ({denied})")

# Migration: Schema-4-Datenbank bekommt spam_learned, ohne den Cache zu verwerfen
import sqlite3  # noqa: E402
d = tempfile.mkdtemp(); path = os.path.join(d, "mailsweep.sqlite3")
con = sqlite3.connect(path)
con.executescript(db.SCHEMA.replace("    spam_learned    TEXT    NOT NULL DEFAULT '',  -- 'spam' | 'ham' | 'skip' (#12)\n", ""))
con.execute("INSERT INTO meta VALUES ('schema','4')")
con.execute("INSERT INTO messages(folder, uid, from_addr, spam_score) VALUES ('INBOX', 1, 'a@b.example', 3.5)"); con.commit(); con.close()
settings.data_dir = d
db.init_db()
with db.connect() as con:
    cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
    row = con.execute("SELECT COUNT(*), MAX(spam_score) FROM messages").fetchone()
    v = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
ok("spam_learned" in cols and tuple(row) == (1, 3.5) and v == "5", f"Migration 4 -> 5 behält Cache und Scores ({tuple(row)}, {v})")
print("LERN-TESTS OK" + (" (echter Rspamd)" if REAL else ""))
