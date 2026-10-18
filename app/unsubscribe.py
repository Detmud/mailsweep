"""Abmelden nach RFC 2369 / RFC 8058, nur mit gültiger, zum Absender passender DKIM-Signatur."""
from __future__ import annotations

import ipaddress
import logging
import re
import smtplib
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import dkim
import dkim.util
import dns.resolver
import httpx

from . import db
from .config import settings
from .domains import org_domain
from .jobs import imap_job

log = logging.getLogger("mailsweep.unsub")

# In Tests ersetzbar (dkimpy-Signatur: dnsfunc(name, timeout=...) -> bytes)
DNSFUNC = None

_URI_RE = re.compile(r"<\s*([^>]+?)\s*>")


def parse_list_unsubscribe(value: str) -> dict[str, list[str]]:
    out = {"https": [], "http": [], "mailto": []}
    for uri in _URI_RE.findall(value or ""):
        uri = uri.strip()
        low = uri.lower()
        if low.startswith("https://"):
            out["https"].append(uri)
        elif low.startswith("http://"):
            out["http"].append(uri)
        elif low.startswith("mailto:"):
            out["mailto"].append(uri)
    return out


def is_one_click(post_value: str) -> bool:
    return "list-unsubscribe=one-click" in (post_value or "").replace(" ", "").lower()


@dataclass
class DkimVerdict:
    passed: bool = False
    aligned: bool = False
    domain: str = ""
    covers_unsub: bool = False
    covers_post: bool = False
    detail: list[str] = field(default_factory=list)

    @property
    def ok_for_oneclick(self) -> bool:
        return self.passed and self.aligned and self.covers_unsub and self.covers_post

    @property
    def ok_for_mailto(self) -> bool:
        return self.passed and self.aligned and self.covers_unsub


def verify_dkim(raw: bytes, from_domain: str) -> DkimVerdict:
    """Prüft jede DKIM-Signatur und merkt sich die beste passende."""
    v = DkimVerdict()
    try:
        headers, _ = dkim.rfc822_parse(raw)
    except Exception as e:
        v.detail.append(f"Mail nicht parsebar: {e}")
        return v
    sigs = [val for name, val in headers if name.lower() == b"dkim-signature"]
    if not sigs:
        v.detail.append("keine DKIM-Signatur")
        return v
    from_org = org_domain(from_domain)
    d = dkim.DKIM(raw, timeout=10)
    best = None
    for idx, sig in enumerate(sigs):
        try:
            tags = dkim.util.parse_tag_value(sig)
        except Exception as e:
            v.detail.append(f"Signatur {idx}: unlesbar ({e})")
            continue
        sdom = tags.get(b"d", b"").decode(errors="replace").strip().lower()
        signed = {h.strip().lower() for h in tags.get(b"h", b"").decode(errors="replace").split(":")}
        try:
            ok = d.verify(idx=idx, dnsfunc=DNSFUNC) if DNSFUNC else d.verify(idx=idx)
        except Exception as e:
            ok = False
            v.detail.append(f"Signatur {idx} ({sdom}): Fehler {e}")
        aligned = bool(sdom) and org_domain(sdom) == from_org
        cand = DkimVerdict(
            passed=ok,
            aligned=aligned,
            domain=sdom,
            covers_unsub="list-unsubscribe" in signed,
            covers_post="list-unsubscribe-post" in signed,
        )
        v.detail.append(
            f"Signatur {idx} d={sdom}: {'gültig' if ok else 'ungültig'}, "
            f"{'passt zum Absender' if aligned else 'fremde Domain'}, "
            f"List-Unsubscribe {'signiert' if cand.covers_unsub else 'NICHT signiert'}"
        )
        score = (cand.passed, cand.aligned, cand.covers_unsub, cand.covers_post)
        if best is None or score > (best.passed, best.aligned, best.covers_unsub, best.covers_post):
            best = cand
    if best:
        best.detail = v.detail
        return best
    return v


class DnsBlocked(ValueError):
    """Der DNS-Server liefert eine Sperradresse (0.0.0.0, ::, 127.x): typisch für DNS-Filter."""


class NonPublicHost(ValueError):
    """Der Host zeigt auf eine nicht öffentliche Adresse (LAN, Link-Local, reserviert)."""


def classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """ok | sinkhole | internal. In Tests ersetzbar."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_loopback:
        return "sinkhole"
    if not ip.is_global or ip.is_multicast:
        return "internal"
    return "ok"


# In Tests ersetzbar
DNS_PORT = 53
DNS_LIFETIME = 8.0


def _resolve(host: str, nameserver: str | None = None) -> list:
    """Löst host auf, entweder über das System oder gezielt über nameserver (A und AAAA)."""
    if not nameserver:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return list(dict.fromkeys(ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos))
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = [nameserver]
    r.port = DNS_PORT
    r.lifetime = DNS_LIFETIME
    ips: list = []
    for rtype in ("A", "AAAA"):
        try:
            ips += [ipaddress.ip_address(a.to_text()) for a in r.resolve(host, rtype)]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass
        except (dns.resolver.LifetimeTimeout, dns.resolver.NoNameservers) as e:
            raise ValueError(f"Ausweich-DNS {nameserver} nicht erreichbar ({type(e).__name__})") from None
    if not ips:
        raise ValueError(f"{host} ist über {nameserver} nicht auflösbar")
    return list(dict.fromkeys(ips))


# In Tests ersetzbar: (host, nameserver) -> [ip, ...]
RESOLVE = _resolve
# In Tests ersetzbar: True oder ssl.SSLContext
TLS_VERIFY: bool | ssl.SSLContext = True


def resolve_public(url: str, nameserver: str | None = None):
    """Gibt (ascii_host, ip) zurück oder wirft DnsBlocked / NonPublicHost."""
    host = urlsplit(url).hostname or ""
    if not host:
        raise ValueError("URL ohne Host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(f"Ungültiger Hostname {host!r}") from None
    ips = RESOLVE(host, nameserver)
    if not ips:
        raise ValueError(f"{host} nicht auflösbar")
    kinds = [classify_ip(ip) for ip in ips]
    via = f" über {nameserver}" if nameserver else ""
    if all(k == "sinkhole" for k in kinds):
        raise DnsBlocked(
            f"Dein DNS-Filter blockiert {host} (Antwort{via}: {ips[0]}). "
            "Die Signatur ist davon nicht betroffen."
        )
    bad = [ip for ip, k in zip(ips, kinds) if k != "ok"]
    if bad:
        raise NonPublicHost(
            f"{host} zeigt{via} auf eine nicht öffentliche Adresse ({bad[0]}), abgelehnt. "
            "Liefert dein DNS-Filter für gesperrte Domains seine eigene IP, ist das dieselbe Ursache."
        )
    ips.sort(key=lambda ip: ip.version)  # IPv4 zuerst
    return host, ips[0]


def one_click_post(url: str, nameserver: str | None = None) -> tuple[bool, str]:
    """RFC-8058-POST. Die Verbindung geht an genau die geprüfte IP, das Zertifikat wird
    gegen den echten Hostnamen geprüft (SNI), damit kein zweites DNS-Ergebnis dazwischenfunkt."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("One-Click nur über HTTPS")
    host, ip = resolve_public(url, nameserver)
    port = parts.port
    ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = ip_host + (f":{port}" if port else "")
    target = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, verify=TLS_VERIFY,
                      headers={"User-Agent": "mailsweep/1.0 (RFC 8058)"}) as c:
        r = c.post(target, data={"List-Unsubscribe": "One-Click"},
                   headers={"Host": host + (f":{port}" if port else "")},
                   extensions={"sni_hostname": host})
    via = f", aufgelöst über {nameserver}" if nameserver else ""
    if 200 <= r.status_code < 300:
        return True, f"HTTP {r.status_code}{via}"
    if 300 <= r.status_code < 400:
        return True, f"HTTP {r.status_code} (Weiterleitung, Abmeldung vermutlich angenommen){via}"
    return False, f"HTTP {r.status_code}{via}"


def send_mailto(uri: str) -> tuple[bool, str]:
    parts = urlsplit(uri)
    to = unquote(parts.path)
    q = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    if not to or "@" not in to:
        return False, "mailto ohne Empfänger"
    msg = EmailMessage()
    msg["From"] = settings.sender_address
    msg["To"] = to
    msg["Subject"] = q.get("subject", "unsubscribe")
    msg.set_content(q.get("body", "unsubscribe"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(settings.imap_user, settings.imap_password)
        s.send_message(msg)
    return True, f"Abmelde-Mail an {to} gesendet"


def _latest_with_header(addr: str):
    with db.connect() as con:
        return con.execute(
            "SELECT folder, uid, from_domain, list_unsub, list_unsub_post FROM messages "
            "WHERE from_addr=? AND list_unsub<>'' ORDER BY date DESC LIMIT 1",
            (addr,),
        ).fetchone()


def unsubscribe(addr: str, method: str = "auto", dns_mode: str = "system") -> dict:
    """method: auto | oneclick | mailto; dns_mode: system | fallback.

    Jede Antwort enthält unter "alternatives", was die UI danach noch anbieten kann,
    damit ein Fehler nie in einer Sackgasse endet."""
    row = _latest_with_header(addr)
    if not row:
        return {"status": "none", "message": "Keine Mail dieses Absenders mit List-Unsubscribe-Header.",
                "alternatives": {"url": None, "url_verified": False, "mailto": False, "fallback_dns": None}}
    links = parse_list_unsubscribe(row["list_unsub"])
    oneclick = bool(links["https"]) and is_one_click(row["list_unsub_post"])

    with imap_job() as conn:
        raw = conn.fetch_full(row["folder"], row["uid"])
    verdict = verify_dkim(raw, row["from_domain"])
    web = links["https"] or links["http"]
    nameserver = settings.unsub_fallback_dns if dns_mode == "fallback" else None
    result: dict = {
        "dkim": asdict(verdict) | {"ok_oneclick": verdict.ok_for_oneclick, "ok_mailto": verdict.ok_for_mailto},
        "alternatives": {
            "url": web[0] if web else None,
            "url_verified": verdict.passed and verdict.aligned,
            "mailto": bool(links["mailto"]) and settings.allow_mailto and verdict.ok_for_mailto,
            "fallback_dns": None,
        },
    }

    def done(ok: bool, status_: str, message: str, via: str, code: str = ""):
        prefix = "[DRY-RUN] " if settings.dry_run and via != "link" else ""
        db.add_log("unsubscribe", addr, f"{prefix}{via}: {message}", ok=ok)
        if ok and via != "link" and not settings.dry_run:
            with db.connect() as con:
                con.execute(
                    "INSERT INTO sender_state(grp, key, unsubscribed_at, unsub_result) VALUES ('addr',?,?,?) "
                    "ON CONFLICT(grp, key) DO UPDATE SET unsubscribed_at=excluded.unsubscribed_at, "
                    "unsub_result=excluded.unsub_result",
                    (addr, int(time.time()), f"{prefix}{via}: {message}"),
                )
        if code in ("dns_blocked", "dns_internal") and not nameserver and settings.unsub_fallback_dns:
            result["alternatives"]["fallback_dns"] = settings.unsub_fallback_dns
        result.update(status=status_, message=prefix + message, via=via, code=code)
        return result

    if dns_mode == "fallback" and not nameserver:
        return done(False, "error", "Kein Ausweich-DNS eingestellt (Einstellungen).", "oneclick", "config")

    def run_oneclick():
        url = links["https"][0]
        try:
            if settings.dry_run:
                resolve_public(url, nameserver)  # DNS-Probleme schon im Testmodus sichtbar machen
                via_ns = f" (aufgelöst über {nameserver})" if nameserver else ""
                return done(True, "ok", f"würde POST an {url} senden{via_ns}", "oneclick")
            ok, msg = one_click_post(url, nameserver)
            return done(ok, "ok" if ok else "error", msg, "oneclick")
        except DnsBlocked as e:
            return done(False, "error", str(e), "oneclick", "dns_blocked")
        except NonPublicHost as e:
            return done(False, "error", str(e), "oneclick", "dns_internal")
        except Exception as e:
            log.exception("One-Click fehlgeschlagen")
            return done(False, "error", f"{type(e).__name__}: {e}", "oneclick", "network")

    def run_mailto():
        if settings.dry_run:
            return done(True, "ok", f"würde Mail an {links['mailto'][0]} senden", "mailto")
        try:
            ok, msg = send_mailto(links["mailto"][0])
            return done(ok, "ok" if ok else "error", msg, "mailto")
        except Exception as e:
            log.exception("Abmelde-Mail fehlgeschlagen")
            return done(False, "error", f"{type(e).__name__}: {e}", "mailto", "smtp")

    if method in ("auto", "oneclick") and oneclick:
        if verdict.ok_for_oneclick:
            return run_oneclick()
        if method == "oneclick":
            return done(False, "refused", "DKIM-Prüfung nicht bestanden, One-Click abgelehnt.", "oneclick", "dkim")

    if method in ("auto", "mailto") and links["mailto"] and settings.allow_mailto:
        if verdict.ok_for_mailto:
            return run_mailto()
        if method == "mailto" or not web:
            return done(False, "refused", "DKIM-Prüfung nicht bestanden, keine Abmelde-Mail gesendet.", "mailto", "dkim")

    if method == "oneclick" and not oneclick:
        return done(False, "refused", "Dieser Absender bietet kein One-Click-Abmelden an.", "oneclick", "unsupported")
    if method == "mailto" and not (links["mailto"] and settings.allow_mailto):
        return done(False, "refused", "Abmelden per Mail ist nicht möglich oder in den Einstellungen aus.", "mailto", "unsupported")

    if web:
        hint = "" if verdict.passed and verdict.aligned else " Achtung: DKIM nicht bestätigt, Link mit Vorsicht öffnen."
        result["url"] = web[0]
        return done(True, "manual", "Kein automatisches Verfahren möglich, Link selbst öffnen." + hint, "link")
    return done(False, "refused", "Keine sichere Abmeldemöglichkeit gefunden.", "none", "dkim" if not verdict.ok_for_mailto else "none")
