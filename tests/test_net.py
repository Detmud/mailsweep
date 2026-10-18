"""Netzwerk-Tests fürs Abmelden, ohne Mailserver: Sinkhole-Erkennung, Ausweich-DNS, IP-Pinning mit TLS.

Aufruf: python tests/test_net.py   (braucht openssl im PATH)
"""
import http.server, ipaddress, os, socket, ssl, subprocess, sys, tempfile, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import dns.message, dns.rrset  # noqa: E402
from app import unsubscribe as U  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
ip = ipaddress.ip_address


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return str(e) or True
    except Exception as e:  # falscher Typ
        return False if not isinstance(e, exc) else True
    return False


# --- 1. Klassifizierung -----------------------------------------------------
for a, want in [("0.0.0.0", "sinkhole"), ("::", "sinkhole"), ("127.0.0.1", "sinkhole"), ("::1", "sinkhole"),
                ("192.168.10.5", "internal"), ("10.0.0.1", "internal"), ("100.64.0.1", "internal"),
                ("169.254.1.1", "internal"), ("224.0.0.1", "internal"), ("fd00::1", "internal"),
                ("::ffff:0.0.0.0", "sinkhole"), ("::ffff:192.168.1.1", "internal"), ("8.8.8.8", "ok"), ("2606:4700::1111", "ok")]:
    ok(U.classify_ip(ip(a)) == want, f"classify {a} -> {want}")

# --- 2. resolve_public mit Stub ----------------------------------------------
table = {}
U.RESOLVE = lambda host, ns=None: [ip(x) for x in table[(host, ns)]]
table[("r.openai.com", None)] = ["0.0.0.0", "::"]
m = raises(lambda: U.resolve_public("https://r.openai.com/x"), U.DnsBlocked)
ok(m and "DNS-Filter" in m and "0.0.0.0" in m, f"0.0.0.0/:: -> DnsBlocked ({m})")
table[("pihole.example", None)] = ["192.168.10.3"]
m = raises(lambda: U.resolve_public("https://pihole.example/x"), U.NonPublicHost)
ok(m and "192.168.10.3" in m, "LAN-IP -> NonPublicHost")
table[("mixed.example", None)] = ["93.184.216.34", "10.0.0.1"]
ok(raises(lambda: U.resolve_public("https://mixed.example/"), U.NonPublicHost), "öffentlich + LAN gemischt -> abgelehnt (Rebinding)")
table[("r.openai.com", "9.9.9.9")] = ["2606:4700::1", "104.18.1.1"]
ok(U.resolve_public("https://r.openai.com/x", "9.9.9.9") == ("r.openai.com", ip("104.18.1.1")), "Ausweich-DNS: IPv4 bevorzugt")
table[("xn--mnchen-3ya.example", None)] = ["93.184.216.34"]
ok(U.resolve_public("https://münchen.example/")[0] == "xn--mnchen-3ya.example", "IDN wird nach Punycode gewandelt")
ok(raises(lambda: U.resolve_public("https:///nohost"), ValueError), "URL ohne Host -> Fehler")
U.RESOLVE = U._resolve

# --- 3. Ausweich-DNS gegen lokalen DNS-Server --------------------------------
def dns_server(answers):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    def loop():
        while True:
            data, peer = sock.recvfrom(4096)
            q = dns.message.from_wire(data)
            resp = dns.message.make_response(q)
            qn, qt = q.question[0].name.to_text(), dns.rdatatype.to_text(q.question[0].rdtype)
            for rr in answers.get((qn, qt), []):
                resp.answer.append(dns.rrset.from_text(*rr))
            if (qn, qt) not in answers and not any(k[0] == qn for k in answers):
                resp.set_rcode(dns.rcode.NXDOMAIN)
            sock.sendto(resp.to_wire(), peer)
    threading.Thread(target=loop, daemon=True).start()
    return sock.getsockname()[1]

U.DNS_PORT = dns_server({
    ("r.openai.com.", "A"): [("r.openai.com.", 60, "IN", "CNAME", "sendgrid.net."),
                             ("sendgrid.net.", 60, "IN", "A", "167.89.118.52")],
    ("r.openai.com.", "AAAA"): [],
})
U.DNS_LIFETIME = 2
got = U._resolve("r.openai.com", "127.0.0.1")
ok(got == [ip("167.89.118.52")], f"Ausweich-DNS folgt CNAME-Kette ({got})")
ok(raises(lambda: U._resolve("gibtsnicht.example", "127.0.0.1"), ValueError), "NXDOMAIN über Ausweich-DNS -> Fehler")
free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); free.bind(("127.0.0.1", 0)); U.DNS_PORT = free.getsockname()[1]; free.close()
U.DNS_LIFETIME = 1
m = raises(lambda: U._resolve("r.openai.com", "127.0.0.1"), ValueError)
ok(m and "nicht erreichbar" in m, f"Ausweich-DNS nicht erreichbar -> klare Meldung ({m})")

# --- 4. IP-Pinning mit TLS gegen lokalen HTTPS-Server ------------------------
d = tempfile.mkdtemp()
run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)
run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1", "-subj", "/CN=mailsweep test CA",
    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=unsub.test")
open(os.path.join(d, "ext"), "w").write("subjectAltName=DNS:unsub.test\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                      "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
run("openssl", "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem", "-days", "1", "-extfile", "ext")

seen = {}

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        seen.update(path=self.path, host=self.headers["Host"],
                    body=self.rfile.read(int(self.headers["Content-Length"])).decode())
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a):
        pass

sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
sctx.load_cert_chain(os.path.join(d, "srv.pem"), os.path.join(d, "srv.key"))
sctx.sni_callback = lambda sock, name, ctx: seen.update(sni=name)
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

resolves = []
def fake_resolve(host, ns=None):
    resolves.append((host, ns))
    return [ip("127.0.0.1")]
U.RESOLVE = fake_resolve
U.classify_ip = lambda a: "ok"          # Loopback hier ausnahmsweise erlauben
U.TLS_VERIFY = ssl.create_default_context(cafile=os.path.join(d, "ca.pem"))

res = U.one_click_post(f"https://unsub.test:{port}/u?id=42", "9.9.9.9")
ok(res[0] and "9.9.9.9" in res[1], f"One-Click über gepinnte IP ({res})")
ok(seen.get("sni") == "unsub.test", f"SNI = echter Hostname ({seen.get('sni')})")
ok(seen.get("host") == f"unsub.test:{port}", f"Host-Header = echter Hostname ({seen.get('host')})")
ok(seen.get("path") == "/u?id=42" and seen.get("body") == "List-Unsubscribe=One-Click", "Pfad und RFC-8058-Body")
ok(resolves == [("unsub.test", "9.9.9.9")], f"genau eine Auflösung, kein zweiter DNS-Lookup ({resolves})")
m = raises(lambda: U.one_click_post(f"https://evil.test:{port}/u"), Exception)
ok(m and "certificate" in str(m).lower(), f"falscher Hostname -> TLS-Prüfung schlägt fehl ({m})")
ok(raises(lambda: U.one_click_post(f"http://unsub.test:{port}/u"), ValueError), "One-Click über http abgelehnt")
srv.shutdown()

# --- 5. Einstellung validiert IP ---------------------------------------------
from app.config import coerce  # noqa: E402
ok(coerce("unsub_fallback_dns", " 1.1.1.1 ") == "1.1.1.1" and coerce("unsub_fallback_dns", "") == "", "Ausweich-DNS: IP oder leer")
ok(raises(lambda: coerce("unsub_fallback_dns", "dns.quad9.net"), ValueError), "Ausweich-DNS: Hostname abgelehnt")
print("NETZ-TESTS OK")
