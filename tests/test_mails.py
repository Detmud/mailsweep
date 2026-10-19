"""Tab Mails (#16): einzelne Mails listen, als Spam melden, als Kein Spam anlernen, umlernen.

Läuft gegen Dovecot wie test_learn.py. Standard ist ein Fake-Rspamd (Scanner und Controller),
mit RSPAMD_URL, RSPAMD_CONTROLLER_URL und RSPAMD_PASSWORD gegen einen echten Rspamd mit Redis.

Aufruf: python tests/test_mails.py   (Dovecot auf 127.0.0.1:10143)
"""
import imaplib, json, os, sys, tempfile, threading, time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer

REAL = os.environ.get("RSPAMD_URL")
FOLDER = "Mailtest"
HOST, PORT, USER, PW = "127.0.0.1", 10143, "test@icloud.test", "geheim"
SECRET = os.environ.get("RSPAMD_PASSWORD", "mailpasswort")

learned: list[tuple[str, bytes]] = []


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
                return self._send(403, {"error": "Unauthorized"})
            kind = self.path[6:]
            if (kind, body) in learned:
                return self._send(208, {"error": f"has been already learned as {kind}, ignore it"})
            # Umlernen wie Rspamd: aus der anderen Klasse austragen
            other = "ham" if kind == "spam" else "spam"
            if (other, body) in learned:
                learned.remove((other, body))
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
from app import main, jobs  # noqa: E402
from app.config import settings  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

WORDS = ("garden river window coffee mountain letter yellow summer pencil travel music orange "
         "basket candle forest planet silver winter bridge market").split()
PHISH = ('Dear customer, your account is suspended!!! Verify now. '
         '<a href="http://198.51.100.7/login.php">https://www.paypal.com/signin</a>')


def mail(frm, subject, body, html=False, n=0, phish=False):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "undisclosed-recipients:;" if phish else USER, subject
    if not phish:
        m["Date"] = formatdate(time.time() - n * 60, localtime=True)
        m["Message-ID"] = make_msgid(domain="x.example")
    text = body + "\n\n" + " ".join(WORDS[(n + i) % len(WORDS)] for i in range(40)) + f" nummer {n}"
    m.set_content(f"<html><body><p>{text}</p></body></html>" if html else text, subtype="html" if html else "plain")
    return m.as_bytes()


def fill(mails):
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
    for raw in mails:
        s.append(FOLDER, None, imaplib.Time2Internaldate(time.time()), raw)
    s.logout()


def count(folder=FOLDER):
    s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
    n = int(s.select(f'"{folder}"', readonly=True)[1][0]); s.logout()
    return n


s = imaplib.IMAP4(HOST, PORT); s.login(USER, PW)
s.delete(FOLDER); s.create(FOLDER); s.logout()
base_trash, base_archive = count("Deleted Messages"), count("Archive")
fill([mail("Anna <anna@private.example>", "Treffen am Samstag", "Hallo, passt dir Samstag um 15 Uhr?", n=1),
      mail("Bank <info@bank.example>", "Ihr Kontoauszug", "Ihr neuer Kontoauszug liegt bereit", n=2),
      mail("Mixed <mixed@shop.example>", "Bestellung versandt", "Ihre Bestellung ist unterwegs", n=3),
      mail("Mixed <mixed@shop.example>", "Gewinnspiel", "Sie haben gewonnen, jetzt Gewinn abholen", n=4),
      mail('"PayPal Service" <a1@paypa1-secure.example>', "URGENT: ACCOUNT SUSPENDED!!!", PHISH, html=True, n=5, phish=True),
      mail('"PayPal Team" <b2@paypa1-alert.example>', "URGENT: VERIFY NOW!!!", PHISH, html=True, n=6, phish=True)])

with TestClient(main.app) as c:
    c.auth = ("u", "pw")
    H = {"X-Mailsweep": "1"}
    msgs = lambda **kw: c.get("/api/messages", params=kw).json()
    act = lambda rows, action: c.post("/api/messages/action", headers=H, json={
        "action": action, "items": [{"folder": r["folder"], "uid": r["uid"]} for r in rows]})
    by_subj = lambda: {r["subject"]: r for r in msgs(show_kept=True, limit=500)["rows"]}
    counters = lambda: (lambda ln: (ln["spam"], ln["ham"]))(c.get("/api/status").json()["spam"]["learn"])

    c.post("/api/action", headers=H, json={"group": "addr", "keys": ["info@bank.example"], "action": "keep"})
    jobs.run_sync()
    # Der Sync lernt die Mail des ausgeblendeten Absenders als Ham an (#12)
    ok(counters() == (0, 1), f"Sync: Bank als Ham angelernt ({counters()})")

    # 1. Liste
    r = msgs()
    ok(r["total"] == 5 and all(x["from_addr"] != "info@bank.example" for x in r["rows"]), f"Liste ohne ausgeblendete Absender ({r['total']})")
    ok(msgs(show_kept=True)["total"] == 6, "Mit ausgeblendeten: 6")
    sp = msgs(only_spam=True, sort="spam")["rows"]
    ok({x["from_addr"] for x in sp} == {"a1@paypa1-secure.example", "b2@paypa1-alert.example"}, f"Nur Spam-Verdacht: die zwei Phishing-Mails ({[x['from_addr'] for x in sp]})")
    ok(msgs(sort="spam")["rows"][0]["spam_score"] >= 6, "Sortierung nach Score")
    ok(msgs(q="Gewinn")["total"] == 1 and msgs(q="shop.example")["total"] == 2, "Suche nach Betreff und Absender")
    ok(msgs(limit=2)["rows"].__len__() == 2 and msgs(limit=2, offset=4)["rows"].__len__() == 1, "Seiten")
    ok(c.get("/api/messages", params={"sort": "evil"}).status_code == 422, "Unbekannte Sortierung abgelehnt")
    keys = set(msgs()["rows"][0])
    ok(keys == {"folder", "uid", "from_addr", "from_name", "subject", "date", "size", "spam_score", "spam_symbols", "spam_learned", "kept"},
       f"Nur Header-Felder, kein Inhalt ({sorted(keys)})")

    # 2. Kein Spam: Ham anlernen, Mail bleibt liegen
    m = by_subj()
    r = act([m["Treffen am Samstag"]], "ham").json()
    ok(r["learned"] == 1 and r["moved"] == 0 and not r["learn_error"], f"Kein Spam: 1 angelernt, nichts verschoben ({r})")
    ok(count() == 6 and by_subj()["Treffen am Samstag"]["spam_learned"] == "ham", "Mail liegt noch, Stand ham")
    ok(counters() == (0, 2), f"Zähler Ham ({counters()})")
    r = act([m["Treffen am Samstag"]], "ham").json()
    ok(r["learned"] == 0 and counters() == (0, 2), f"Zweimal Kein Spam lernt nicht doppelt ({r})")

    # 3. Umlernen: dieselbe Mail jetzt als Spam melden
    r = act([m["Treffen am Samstag"]], "spam").json()
    ok(r["learned"] == 1 and r["moved"] == 1 and not r["learn_error"], f"Umlernen Ham -> Spam ({r})")
    ok(counters() == (1, 1), f"Zähler umgebucht ({counters()})")
    if not REAL:
        ok(not any(k == "ham" and b"Samstag" in b for k, b in learned), "Fake-Rspamd hat die Mail aus Ham ausgetragen")

    # 4. Nur eine Mail eines Absenders: die andere bleibt
    r = act([m["Gewinnspiel"]], "spam").json()
    ok(r["moved"] == 1 and r["learned"] == 1, f"Einzelne Mail von Mixed gemeldet ({r})")
    left = [x["subject"] for x in msgs(q="shop.example")["rows"]]
    ok(left == ["Bestellung versandt"], f"Andere Mail des Absenders bleibt ({left})")
    ok(count() == 4 and count("Deleted Messages") == base_trash + 2, "Server: 2 im Papierkorb")

    # 5. Mehrfachauswahl Spam, Archivieren und Löschen ohne Anlernen
    r = act(msgs(only_spam=True)["rows"], "spam").json()
    ok(r["moved"] == 2 and r["learned"] == 2 and counters() == (4, 1), f"Mehrfachauswahl: 2 gemeldet ({r}, {counters()})")
    r = act([by_subj()["Bestellung versandt"]], "archive").json()
    ok(r["moved"] == 1 and r["learned"] == 0 and count("Archive") == base_archive + 1, f"Archivieren lernt nichts ({r})")
    dn = {x["key"]: x for x in c.get("/api/done").json()["rows"]}
    ok(dn["mixed@shop.example"]["moves"] == {**dn["mixed@shop.example"]["moves"], "trash": 1, "archive": 1}, "Tab Erledigt zählt Einzelaktionen mit")

    # 6. Testmodus: Spam weder angelernt noch verschoben, Ham wird angelernt
    fill([mail("Neu <neu@private.example>", "Hallo aus dem Urlaub", "Viele Grüße vom Strand", n=7),
          mail("Neu2 <neu2@private.example>", "Rechnung", "Anbei die Rechnung", n=8)])
    jobs.run_sync()
    c.post("/api/settings", headers=H, json={"values": {"dry_run": True}})
    m = by_subj()
    before = list(learned)
    r = act([m["Rechnung"]], "spam").json()
    ok(r["dry_run"] and r["moved"] == 1 and r["learned"] == 0 and count() == 3 and learned == before, f"Testmodus Spam: nichts passiert ({r})")
    r = act([m["Hallo aus dem Urlaub"]], "ham").json()
    ok(r["learned"] == 1 and count() == 3, f"Testmodus Ham: angelernt ({r})")
    c.post("/api/settings", headers=H, json={"values": {"dry_run": False}})

    # 7. Grenzfälle
    r = act([{"folder": FOLDER, "uid": 99999}], "trash").json()
    ok(r["moved"] == 0, "Unbekannte UID: nichts verschoben")
    ok(c.post("/api/messages/action", headers=H, json={"action": "trash", "items": [{"folder": FOLDER, "uid": i} for i in range(1, 502)]}).status_code == 422, "Mehr als 500 abgelehnt")
    ok(c.post("/api/messages/action", headers=H, json={"action": "nuke", "items": [{"folder": FOLDER, "uid": 1}]}).status_code == 422, "Unbekannte Aktion abgelehnt")
    ok(c.post("/api/messages/action", json={"action": "trash", "items": [{"folder": FOLDER, "uid": 1}]}).status_code == 403, "Ohne X-Mailsweep-Header abgelehnt")
    settings.rspamd_password = ""
    rr = act([m["Rechnung"]], "ham")
    ok(rr.status_code == 400 and "RSPAMD_PASSWORD" in rr.json()["detail"], f"Kein Spam ohne Passwort: klare Meldung ({rr.json()})")
    settings.rspamd_password = SECRET
    settings.spam_filter = False
    ok(act([m["Rechnung"]], "spam").status_code == 400, "Spam melden bei ausgeschaltetem Spamfilter abgelehnt")
    r = act([m["Rechnung"]], "trash").json()
    ok(r["moved"] == 1 and count() == 2, "Löschen geht auch ohne Spamfilter")
    settings.spam_filter = True
    lg = c.get("/api/log").json()["log"]
    ok(any("einzelne Mail" in x["target"] for x in lg), "Protokoll nennt Einzelaktionen")

print("MAIL-TESTS OK" + (" (echter Rspamd)" if REAL else ""))
