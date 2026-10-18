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
from urllib.parse import parse_qs, unquote, urlsplit

import dkim
import dkim.util
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


def _check_public_host(url: str) -> None:
    host = urlsplit(url).hostname or ""
    if not host:
        raise ValueError("URL ohne Host")
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"{host} zeigt auf eine interne Adresse ({ip}), abgelehnt")


def one_click_post(url: str) -> tuple[bool, str]:
    _check_public_host(url)
    with httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": "mailsweep/1.0 (RFC 8058)"}) as c:
        r = c.post(url, data={"List-Unsubscribe": "One-Click"})
    if 200 <= r.status_code < 300:
        return True, f"HTTP {r.status_code}"
    if 300 <= r.status_code < 400:
        return True, f"HTTP {r.status_code} (Weiterleitung, Abmeldung vermutlich angenommen)"
    return False, f"HTTP {r.status_code}"


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


def unsubscribe(addr: str, method: str = "auto") -> dict:
    """method: auto | oneclick | mailto. Gibt bei reinen Web-Links nur die URL zurück."""
    row = _latest_with_header(addr)
    if not row:
        return {"status": "none", "message": "Keine Mail dieses Absenders mit List-Unsubscribe-Header."}
    links = parse_list_unsubscribe(row["list_unsub"])
    oneclick = bool(links["https"]) and is_one_click(row["list_unsub_post"])

    with imap_job() as conn:
        raw = conn.fetch_full(row["folder"], row["uid"])
    verdict = verify_dkim(raw, row["from_domain"])
    result: dict = {"dkim": asdict(verdict) | {"ok_oneclick": verdict.ok_for_oneclick, "ok_mailto": verdict.ok_for_mailto}}

    def done(ok: bool, status_: str, message: str, via: str):
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
        result.update(status=status_, message=prefix + message, via=via)
        return result

    try:
        if method in ("auto", "oneclick") and oneclick:
            if not verdict.ok_for_oneclick:
                if method == "oneclick":
                    return done(False, "refused", "DKIM-Prüfung nicht bestanden, One-Click abgelehnt.", "oneclick")
            else:
                if settings.dry_run:
                    return done(True, "ok", f"würde POST an {links['https'][0]} senden", "oneclick")
                ok, msg = one_click_post(links["https"][0])
                return done(ok, "ok" if ok else "error", msg, "oneclick")

        if method in ("auto", "mailto") and links["mailto"] and settings.allow_mailto:
            if not verdict.ok_for_mailto:
                if method == "mailto" or not (links["https"] or links["http"]):
                    return done(False, "refused", "DKIM-Prüfung nicht bestanden, keine Abmelde-Mail gesendet.", "mailto")
            else:
                if settings.dry_run:
                    return done(True, "ok", f"würde Mail an {links['mailto'][0]} senden", "mailto")
                ok, msg = send_mailto(links["mailto"][0])
                return done(ok, "ok" if ok else "error", msg, "mailto")

        web = links["https"] or links["http"]
        if web:
            result["url"] = web[0]
            hint = "" if verdict.passed and verdict.aligned else " Achtung: DKIM nicht bestätigt, Link mit Vorsicht öffnen."
            return done(True, "manual", "Kein automatisches Verfahren möglich, Link selbst öffnen." + hint, "link")
        return done(False, "refused", "Keine sichere Abmeldemöglichkeit gefunden.", "none")
    except Exception as e:
        log.exception("Abmelden fehlgeschlagen")
        return done(False, "error", str(e), method)
