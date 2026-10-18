"""Header-Parser ohne Mailserver: DKIM-Ergebnis aus (ARC-)Authentication-Results (#1).

Aufruf: python tests/test_parse.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ["TRUSTED_AUTHSERV_IDS"] = "icloud.com"
from app.imap import parse_headers, authserv_id, is_trusted_authserv  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))


def dkim(headers: str, frm: str = "news@mail.shop.example"):
    raw = (headers.strip("\n").replace("\n", "\r\n") + f"\r\nFrom: Shop <{frm}>\r\nSubject: x\r\n\r\n").encode()
    h = parse_headers(1, 100, 0, raw)
    return h.auth_dkim, h.auth_dkim_domain


# iCloud: je Prüfung ein eigener Header, der DKIM-Header steht nicht oben
icloud = """Authentication-Results: bimi.icloud.com; bimi=skipped reason="insufficient dmarc"
Authentication-Results: dmarc.icloud.com; dmarc=pass header.from=shop.example
Authentication-Results: dkim-verifier.icloud.com;
\tdkim=pass (2048-bit key) header.d=shop.example header.i=@shop.example header.b=AbCdEf12
Authentication-Results: spf.icloud.com; spf=pass (spf.icloud.com: domain of bounce@shop.example designates 1.2.3.4 as permitted sender) smtp.mailfrom=bounce@shop.example"""
ok(dkim(icloud) == ("pass", "shop.example"), f"iCloud-Format mit mehreren Headern -> pass ({dkim(icloud)})")

# Gefälschter Header mit fremder authserv-id wird ignoriert, wenn iCloud selbst etwas sagt
forged = """Authentication-Results: dkim-verifier.icloud.com; dkim=fail header.d=shop.example
Authentication-Results: mx.evil.example; dkim=pass header.d=shop.example"""
ok(dkim(forged) == ("fail", "shop.example"), f"fremde authserv-id ignoriert ({dkim(forged)})")

# Fremd signiert (Spoofing-Muster)
foreign = "Authentication-Results: dkim-verifier.icloud.com; dkim=pass header.d=sendgrid.info"
ok(dkim(foreign) == ("pass-unaligned", "sendgrid.info"), "fremde Signatur -> pass-unaligned")

# Zwei Signaturen, eine passt zum Absender
two = "Authentication-Results: dkim-verifier.icloud.com; dkim=pass header.d=sendgrid.info; dkim=pass header.d=shop.example"
ok(dkim(two) == ("pass", "shop.example"), "mehrere Signaturen: passende gewinnt")

# Kein vertrauenswürdiger Header: Verhalten wie bisher (oberster Header)
other = """Authentication-Results: mx.test; dkim=pass header.d=shop.example
Authentication-Results: older.example; dkim=fail header.d=shop.example"""
ok(dkim(other) == ("pass", "shop.example"), "ohne vertrauten Header gilt der oberste")

# dkim-atps und authserv-id 'dkim-verifier' dürfen nicht als dkim=... zählen
atps = "Authentication-Results: dkim-verifier.icloud.com; dkim-atps=neutral"
ok(dkim(atps) == ("", ""), f"dkim-atps ist kein DKIM-Ergebnis ({dkim(atps)})")

# ARC-Fallback, wenn gar kein Authentication-Results da ist
arc = "ARC-Authentication-Results: i=1; mx01.mail.icloud.com; dkim=pass header.d=shop.example; spf=pass"
ok(dkim(arc) == ("pass", "shop.example"), "ARC-Authentication-Results als Fallback")

ok(dkim("") == ("", ""), "ohne Header -> leer (Anzeige ?)")

# authserv-id-Erkennung
ok(authserv_id("i=2; mx01.mail.icloud.com; dkim=pass") == "mx01.mail.icloud.com", "authserv-id aus ARC mit i=")
ok(authserv_id("example.org 1; none") == "example.org", "authserv-id mit Versionsnummer")
ok(is_trusted_authserv("dkim-verifier.icloud.com") and is_trusted_authserv("icloud.com"), "icloud.com vertraut")
ok(not is_trusted_authserv("icloud.com.evil.example") and not is_trusted_authserv("fakeicloud.com"), "Suffix-Tricks nicht vertraut")
print("PARSE-TESTS OK")
