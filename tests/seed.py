"""Befüllt einen Test-IMAP-Server mit realistischen Newsletter-Mails (inkl. DKIM)."""
import imaplib, sys, time, json, random
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timedelta, timezone
import dkim
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

host, port, user, pw, keyout = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
import base64
json.dump({"p": base64.b64encode(pub).decode()}, open(keyout, "w"))

def mk(frm, subj, days, unsub=None, post=None, sign=None, ar=None, extra_h=None):
    m = EmailMessage()
    m["From"] = frm; m["To"] = user; m["Subject"] = subj
    m["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days))
    m["Message-ID"] = make_msgid()
    if unsub: m["List-Unsubscribe"] = unsub
    if post: m["List-Unsubscribe-Post"] = post
    if unsub: m["List-Id"] = "Newsletter <news.list.example>"
    m.set_content("Hallo,\n" + "Inhalt " * random.randint(20, 400))
    raw = m.as_bytes()
    if sign:
        heads = [b"from", b"to", b"subject", b"date", b"message-id"] + ([b"list-unsubscribe"] if unsub else []) + ([b"list-unsubscribe-post"] if post else [])
        sig = dkim.sign(raw, b"sel", sign.encode(), priv, include_headers=heads)
        raw = sig + raw
    if ar:
        raw = f"Authentication-Results: mx.test; {ar}\r\n".encode() + raw
    return raw

c = imaplib.IMAP4(host, port); c.login(user, pw)
c.create('"Newsletter &AMQ-lt"')  # "Newsletter Ält" in mUTF-7
from email.utils import parsedate_to_datetime
import re as _re
def put(raw, folder="INBOX"):
    m = _re.search(rb"^Date: (.+?)\r?$", raw, _re.M)
    try: ts = parsedate_to_datetime(m.group(1).decode()).timestamp()
    except Exception: ts = time.time() - 86400 * 400
    c.append(f'"{folder}"' if " " in folder else folder, None, imaplib.Time2Internaldate(ts), raw)

for i in range(30):
    put(mk('"Shop A" <newsletter@shop-a.example>', f"Angebot der Woche {i}", i * 2,
           unsub="<https://unsub.shop-a.example/u?id=42>, <mailto:unsub@shop-a.example?subject=unsubscribe>",
           post="List-Unsubscribe=One-Click", sign="shop-a.example", ar="dkim=pass header.d=shop-a.example; spf=pass"))
for i in range(10):
    put(mk('=?utf-8?q?M=C3=BCller_Vereinsnachrichten?= <news@mail.verein.example>', f"=?utf-8?q?Vereinsbrief_N=C2=B0{i}_=E2=80=93_Gr=C3=BC=C3=9Fe?=", i * 7,
           unsub="<mailto:abmelden@verein.example?subject=Abmelden>", sign="verein.example", ar="dkim=pass header.i=@verein.example"))
for i in range(5):
    put(mk('"Gewinn" <promo@spoof.example>', f"Sie haben gewonnen {i}", i,
           unsub="<https://evil.example/x>", post="List-Unsubscribe=One-Click", sign="evil.example", ar="dkim=pass header.d=evil.example"))
for i in range(4):
    put(mk('"Linkonly" <info@linkonly.example>', f"Info {i}", i * 3, unsub="<https://linkonly.example/abmelden?u=1>", ar="dkim=none"))
for i in range(3):
    put(mk('"Anna" <anna@private.example>', f"Grillen am Samstag? {i}", i * 20))
# zweite Adresse derselben Organisation
for i in range(6):
    put(mk('"Shop A Service" <service@mail.shop-a.example>', f"Ihre Bestellung {i}", i * 5, sign="shop-a.example", ar="dkim=pass header.d=shop-a.example"))
# kaputte Header
put(b"From: =?broken?= <<<\r\nSubject: =?utf-8?b?!!!?=\r\nDate: kein Datum\r\n\r\nx\r\n")
put(mk('"Shop A" <newsletter@shop-a.example>', "Im Unterordner", 1), "Newsletter &AMQ-lt")
c.logout()
print("seeded")
