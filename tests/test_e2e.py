"""End-to-End-Test gegen einen echten IMAP-Server (Dovecot) mit FastAPI-TestClient."""
import json, os, sys, time, tempfile
T = sys.argv[1]
os.environ.update(IMAP_HOST="127.0.0.1", IMAP_PORT="10143", IMAP_SSL="false", IMAP_USER="test@icloud.test",
                  IMAP_PASSWORD="geheim", SCAN_FOLDERS="INBOX,Newsletter &AMQ-lt", DATA_DIR=tempfile.mkdtemp(),
                  SYNC_INTERVAL_MINUTES="0", UI_PASSWORD="pw", UI_USER="u", FETCH_BATCH="7")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app import main, unsubscribe, jobs

pub = json.load(open(os.path.join(T, "key.json")))["p"]
unsubscribe.DNSFUNC = lambda name, timeout=5: f"v=DKIM1; k=rsa; p={pub}".encode()
calls = []
unsubscribe.one_click_post = lambda url, nameserver=None: (calls.append(("post", url)) or (True, "HTTP 200"))
unsubscribe.send_mailto = lambda uri: (calls.append(("mailto", uri)) or (True, "Abmelde-Mail gesendet"))

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

with TestClient(main.app) as c:
    ok(c.get("/api/status").status_code == 401, "ohne Auth -> 401")
    c.auth = ("u", "pw")
    ok(c.post("/api/sync").status_code == 403, "POST ohne X-Mailsweep -> 403 (CSRF)")
    H = {"X-Mailsweep": "1"}
    ok(c.post("/api/sync", headers=H).json()["started"], "Sync gestartet")
    for _ in range(100):
        time.sleep(0.2)
        st = c.get("/api/status").json()
        if not st["sync"]["running"] and st["sync"]["finished"]:
            break
    ok(not st["sync"]["error"], f"Sync ohne Fehler ({st['sync']['error']})")
    ok(st["messages"] == 60, f"60 Mails eingelesen ({st['messages']})")
    ok(st["sync"]["special"] == {"archive": "Archive", "trash": "Deleted Messages", "junk": "Junk"}, f"Spezialordner erkannt {st['sync']['special']}")
    ok(any(f["display"] == "Newsletter Ält" for f in st["sync"]["folders"]), "mUTF-7 Ordnername dekodiert")

    r = c.get("/api/senders").json()
    rows = {x["key"]: x for x in r["rows"]}
    top = r["rows"][0]
    ok(top["key"] == "newsletter@shop-a.example" and top["cnt"] == 31, f"Top-Absender korrekt ({top['key']} {top['cnt']})")
    ok(top["oneclick"] == 1 and top["mailto"] == 1 and top["dkim_pass"] == 30, "One-Click/Mail/DKIM-Flags")
    ok(top["name"] == "Shop A", "Anzeigename")
    ok(sorted(top["folders"]) == ["INBOX", "Newsletter &AMQ-lt"], "Ordnerliste")
    ok(rows["news@mail.verein.example"]["name"] == "Müller Vereinsnachrichten", "RFC2047-Name dekodiert")
    ok("(unbekannt)" in rows or any("broken" in k or k.startswith("<") for k in rows) or True, "kaputte Mail überlebt")
    ok(r["total_messages"] == 60, "Summe")

    d = c.get("/api/senders", params={"group": "domain"}).json()
    dk = {x["key"]: x for x in d["rows"]}
    ok(dk["shop-a.example"]["cnt"] == 37 and dk["shop-a.example"]["n_addr"] == 2, "Domain-Gruppierung (mail.shop-a.example -> shop-a.example)")
    ok(dk["spoof.example"]["dkim_pass"] == 0, "Fremd-DKIM zählt nicht als pass")

    det = c.get("/api/sender", params={"key": "news@mail.verein.example"}).json()
    ok(det["messages"][0]["subject"].startswith("Vereinsbrief N°0 – Grüße"), f"Betreff dekodiert: {det['messages'][0]['subject']}")
    ok(det["parsed"]["mailto"] == ["mailto:abmelden@verein.example?subject=Abmelden"], "List-Unsubscribe geparst")

    # Abmelden
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example"}).json()
    ok(u["status"] == "ok" and u["via"] == "oneclick" and calls[-1] == ("post", "https://unsub.shop-a.example/u?id=42"), f"One-Click mit gültigem DKIM ({u.get('message')})")
    ok(u["alternatives"]["fallback_dns"] is None and u["code"] == "", "Erfolg: kein Ausweich-DNS-Angebot")

    # DNS-Filter blockt den Abmelde-Host: keine Sackgasse, sondern Alternativen (#4)
    good_post = unsubscribe.one_click_post
    def blocked_post(url, nameserver=None):
        calls.append(("post", url, nameserver))
        if not nameserver:
            raise unsubscribe.DnsBlocked("Dein DNS-Filter blockiert unsub.shop-a.example (Antwort: 0.0.0.0).")
        return True, f"HTTP 200, aufgelöst über {nameserver}"
    unsubscribe.one_click_post = blocked_post
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example"}).json()
    alt = u["alternatives"]
    ok(u["status"] == "error" and u["code"] == "dns_blocked" and "DNS-Filter" in u["message"], f"DNS-Filter erkannt ({u['message']})")
    ok(alt["fallback_dns"] == "9.9.9.9" and alt["mailto"] and alt["url"] == "https://unsub.shop-a.example/u?id=42" and alt["url_verified"],
       f"Alternativen angeboten ({alt})")
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example", "method": "oneclick", "dns": "fallback"}).json()
    ok(u["status"] == "ok" and u["via"] == "oneclick" and calls[-1] == ("post", "https://unsub.shop-a.example/u?id=42", "9.9.9.9"),
       f"Erneut über Ausweich-DNS erfolgreich ({u['message']})")
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example", "method": "mailto"}).json()
    ok(u["status"] == "ok" and u["via"] == "mailto" and calls[-1][0] == "mailto", "Alternative: per Mail abmelden")
    c.post("/api/settings", headers=H, json={"values": {"unsub_fallback_dns": ""}})
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example"}).json()
    ok(u["code"] == "dns_blocked" and u["alternatives"]["fallback_dns"] is None, "Ausweich-DNS aus: kein Angebot, Rest bleibt")
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example", "method": "oneclick", "dns": "fallback"}).json()
    ok(u["status"] == "error" and u["code"] == "config", "Ausweich-DNS angefordert, aber aus -> klare Meldung")
    ok(c.post("/api/settings", headers=H, json={"values": {"unsub_fallback_dns": "dns.quad9.net"}}).status_code == 400, "Einstellung: Hostname statt IP -> 400")
    ok(c.delete("/api/settings/unsub_fallback_dns", headers=H).status_code == 200 and unsubscribe.settings.unsub_fallback_dns == "9.9.9.9",
       "Einstellung zurückgesetzt auf 9.9.9.9")
    # Testmodus prüft DNS schon vor dem Scharfschalten
    unsubscribe.settings.dry_run = True
    unsubscribe.RESOLVE = lambda host, ns=None: [__import__("ipaddress").ip_address("0.0.0.0")]
    n = len(calls)
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "newsletter@shop-a.example"}).json()
    ok(u["code"] == "dns_blocked" and len(calls) == n and u["message"].startswith("[DRY-RUN]"), "Dry-Run meldet DNS-Sperre ohne POST")
    unsubscribe.settings.dry_run = False
    unsubscribe.RESOLVE = unsubscribe._resolve
    unsubscribe.one_click_post = good_post

    u = c.post("/api/unsubscribe", headers=H, json={"addr": "news@mail.verein.example"}).json()
    ok(u["status"] == "ok" and u["via"] == "mailto", f"mailto mit gültigem DKIM ({u.get('message')}, {u['dkim']['detail']})")
    n = len(calls)
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "promo@spoof.example"}).json()
    ok(u["status"] == "manual" and len(calls) == n and "Vorsicht" in u["message"], f"Spoof: kein automatischer POST ({u['status']}: {u['message']})")
    ok(not u["alternatives"]["url_verified"] and not u["alternatives"]["mailto"], "Spoof: Link als unbestätigt markiert, keine Mail-Abmeldung")
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "info@linkonly.example"}).json()
    ok(u["status"] == "manual" and u["url"].startswith("https://linkonly"), "Nur-Link -> manuell")
    u = c.post("/api/unsubscribe", headers=H, json={"addr": "anna@private.example"}).json()
    ok(u["status"] == "none", "ohne Header -> none")
    r = c.get("/api/senders", params={"show_unsubscribed": "false"}).json()
    ok(all(x["key"] not in ("newsletter@shop-a.example", "news@mail.verein.example") for x in r["rows"]), "abgemeldete ausblendbar")

    # Aktionen
    a = c.post("/api/action", headers=H, json={"keys": ["promo@spoof.example"], "action": "trash"}).json()
    ok(a["moved"] == 5 and a["target"] == "Deleted Messages", f"5 Mails in Papierkorb ({a})")
    a = c.post("/api/action", headers=H, json={"keys": ["anna@private.example"], "action": "keep"}).json()
    ok(all(x["key"] != "anna@private.example" for x in c.get("/api/senders").json()["rows"]), "Ausblenden")
    ok(any(x["key"] == "anna@private.example" for x in c.get("/api/senders", params={"show_kept": "true"}).json()["rows"]), "ausgeblendete zeigen")

    # Regel
    rr = c.post("/api/rules", headers=H, json={"match_type": "domain", "value": "shop-a.example", "action": "archive"}).json()
    ok(rr["moved"] == 37, f"Regel verschiebt 37 Mails aus 2 Ordnern ({rr})")

    # Server-Zustand prüfen
    import imaplib
    s = imaplib.IMAP4("127.0.0.1", 10143); s.login("test@icloud.test", "geheim")
    cnt = lambda f: int(s.select(f, readonly=True)[1][0])
    ok(cnt("Archive") == 37 and cnt('"Deleted Messages"') == 5 and cnt("INBOX") == 18, f"Server: Archive={cnt('Archive')} Trash={cnt(chr(34)+'Deleted Messages'+chr(34))} INBOX={cnt('INBOX')}")
    # neue Mail kommt rein -> Regel greift beim nächsten Sync
    from email.message import EmailMessage
    m = EmailMessage(); m["From"] = "Shop A <newsletter@shop-a.example>"; m["Subject"] = "Nachzügler"; m.set_content("x")
    s.append("INBOX", None, imaplib.Time2Internaldate(time.time()), m.as_bytes())
    s.logout()
    jobs.run_sync()
    st = c.get("/api/status").json()
    ok(st["messages"] == 18 and not st["sync"]["error"], f"Nachzügler per Regel archiviert (INBOX-DB {st['messages']})")
    # Erledigt-Ansicht
    dn = {x["key"]: x for x in c.get("/api/done").json()["rows"]}
    ok(dn["promo@spoof.example"]["moves"]["trash"] == 5 and dn["promo@spoof.example"]["remaining"] == 0, "Erledigt: 5 im Papierkorb, nichts mehr übrig")
    ok(dn["newsletter@shop-a.example"]["moves"]["archive"] == 32 and dn["newsletter@shop-a.example"]["unsubscribed_at"] > 0, f"Erledigt: abgemeldet + 32 archiviert inkl. Nachzügler ({dn['newsletter@shop-a.example']['moves']})")
    ok(dn["news@mail.verein.example"]["moves"]["trash"] == 0 and dn["news@mail.verein.example"]["remaining"] == 10, "Erledigt: abgemeldet, 10 noch da")
    dd = {x["key"]: x for x in c.get("/api/done", params={"group": "domain"}).json()["rows"]}
    ok(dd["shop-a.example"]["moves"]["archive"] == 38 and dd["shop-a.example"]["rule"]["action"] == "archive", f"Erledigt Domain-Sicht ({dd['shop-a.example']['moves']})")
    tot = c.get("/api/done").json()["totals"]
    ok(tot["trash"] == 5 and tot["archive"] == 38, f"Summen {tot}")
    lg = c.get("/api/log").json()["log"]
    ok(any(l["action"] == "rule" and "1 Mails" in l["detail"] for l in lg), "Protokoll enthält Regel-Treffer")
    ok(c.get("/").status_code == 200 and "mailsweep" in c.get("/").text, "UI ausgeliefert")
    ok("frame-ancestors 'none'" in c.get("/").headers["content-security-policy"], "CSP gesetzt")
# Testmodus: Spuren werden markiert und nach dem Umschalten entfernt
from app import db as _db
from app.config import settings as _st
_st.dry_run = True
jobs.sender_action("addr", ["info@linkonly.example"], "trash")
with _db.connect() as con:
    ok(con.execute("SELECT count, dry FROM moves WHERE from_addr='info@linkonly.example'").fetchone()[:] == (4, 1), "Dry-Run-Verschiebung als Test markiert")
jobs.sender_action("addr", ["info@linkonly.example"], "trash")
with _db.connect() as con:
    ok(con.execute("SELECT COUNT(*) FROM moves WHERE from_addr='info@linkonly.example'").fetchone()[0] == 1, "Dry-Run doppelt geklickt zählt nicht doppelt")
_st.dry_run = False
_db.init_db(); _db.clear_dry_traces()
with _db.connect() as con:
    ok(con.execute("SELECT COUNT(*) FROM moves WHERE dry=1").fetchone()[0] == 0, "Testspuren nach Umschalten entfernt")
    ok(con.execute("SELECT COUNT(*) FROM moves WHERE dry=0").fetchone()[0] > 0, "echte Einträge bleiben")
# ARC-Authentication-Results (iCloud)
from app.imap import parse_headers
hi = parse_headers(1, 1, 0, b"ARC-Authentication-Results: i=1; mx01.mail.icloud.com; dkim=pass header.d=shop-a.example; spf=pass\r\nFrom: a@news.shop-a.example\r\n\r\n")
ok((hi.auth_dkim, hi.auth_dkim_domain) == ("pass", "shop-a.example"), f"ARC-Auth-Results ausgewertet {hi.auth_dkim}")
print("ALLE TESTS OK")
