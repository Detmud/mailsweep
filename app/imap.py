"""Dünne, robuste Schicht über imaplib, abgestimmt auf iCloud (imap.mail.me.com)."""
from __future__ import annotations

import base64
import imaplib
import logging
import re
import ssl
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from .config import settings
from .domains import org_domain

log = logging.getLogger("mailsweep.imap")

# imaplib begrenzt eine Antwortzeile auf 1 MB. Ein UID SEARCH ALL über
# 200.000 Mails ist größer, daher großzügig anheben.
imaplib._MAXLINE = 64 * 1024 * 1024

HEADER_FIELDS = (
    "FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID AUTHENTICATION-RESULTS ARC-AUTHENTICATION-RESULTS"
)
FETCH_ITEMS = f"(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_MSG_START = re.compile(rb"^\d+ \(")
_UID_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_DKIM_AR_RE = re.compile(r"\bdkim=(\w+)([^;]*)", re.I)
_HEADER_D_RE = re.compile(r"\bheader\.(?:d|i)=@?([A-Za-z0-9._-]+)", re.I)


class ImapError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def quote_mbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_mutf7(raw: str) -> str:
    """IMAP modified UTF-7 (RFC 3501, 5.1.3) nach Unicode."""
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "&":
            j = raw.find("-", i)
            if j == -1:
                out.append(raw[i:])
                break
            chunk = raw[i + 1 : j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(raw[i : j + 1])
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def uid_set(uids) -> str:
    """Kompakte UID-Menge, z.B. 1:5,9,12:14."""
    uids = sorted(set(int(u) for u in uids))
    parts, start, prev = [], None, None
    for u in uids:
        if start is None:
            start = prev = u
        elif u == prev + 1:
            prev = u
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = u
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


@dataclass
class HeaderInfo:
    uid: int
    size: int
    date: int
    from_addr: str
    from_name: str
    from_domain: str
    org_domain: str
    subject: str
    list_unsub: str
    list_unsub_post: str
    list_id: str
    auth_dkim: str
    auth_dkim_domain: str


_parser = BytesParser(policy=policy.default)


def _safe_header(msg, name: str) -> str:
    try:
        val = msg.get(name)
        return "" if val is None else str(val)
    except Exception:
        try:
            raw = msg.get_all(name, failobj=[])
            return str(raw[0]) if raw else ""
        except Exception:
            return ""


def _parse_auth_results(msg, from_org: str) -> tuple[str, str]:
    """Wertet den obersten Authentication-Results-Header aus (den setzt der empfangende Server)."""
    values = []
    # iCloud setzt je nach Weg nur ARC-Authentication-Results; der oberste Eintrag stammt vom eigenen Server.
    for hname in ("Authentication-Results", "ARC-Authentication-Results"):
        try:
            values = msg.get_all(hname) or []
        except Exception:
            values = []
        if values:
            break
    if not values:
        return "", ""
    top = str(values[0])
    results = []
    for m in _DKIM_AR_RE.finditer(top):
        res = m.group(1).lower()
        dm = _HEADER_D_RE.search(m.group(2))
        results.append((res, dm.group(1).lower() if dm else ""))
    if not results:
        return "", ""
    for res, dom in results:
        if res == "pass" and dom and org_domain(dom) == from_org:
            return "pass", dom
    for res, dom in results:
        if res == "pass":
            return "pass-unaligned", dom
    return results[0]


def parse_headers(uid: int, size: int, internaldate: int, raw: bytes) -> HeaderInfo:
    msg = _parser.parsebytes(raw or b"", headersonly=True)

    from_raw = _safe_header(msg, "From")
    name, addr = "", ""
    try:
        addrs = msg["From"].addresses if msg["From"] is not None else ()
        if addrs:
            name, addr = addrs[0].display_name or "", addrs[0].addr_spec or ""
    except Exception:
        pass
    if not addr:
        name2, addr = parseaddr(from_raw)
        name = name or name2
    addr = (addr or "").strip().lower()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    org = org_domain(domain) if domain else ""

    date = internaldate
    if not date:
        try:
            date = int(parsedate_to_datetime(_safe_header(msg, "Date")).timestamp())
        except Exception:
            date = 0

    dkim_res, dkim_dom = _parse_auth_results(msg, org)

    return HeaderInfo(
        uid=uid,
        size=size,
        date=date,
        from_addr=addr or "(unbekannt)",
        from_name=(name or "").strip()[:200],
        from_domain=domain,
        org_domain=org,
        subject=_safe_header(msg, "Subject").replace("\r", " ").replace("\n", " ")[:300],
        list_unsub=" ".join(_safe_header(msg, "List-Unsubscribe").split())[:2000],
        list_unsub_post=_safe_header(msg, "List-Unsubscribe-Post").strip()[:200],
        list_id=_safe_header(msg, "List-Id").strip()[:300],
        auth_dkim=dkim_res,
        auth_dkim_domain=dkim_dom,
    )


def split_fetch_response(data) -> list[tuple[bytes, bytes]]:
    """Zerlegt die imaplib-FETCH-Antwort in (Metadaten, Literal)-Paare.

    imaplib liefert [(b'1 (UID 5 ... {123}', b'<literal>'), b' UID 5)', ...].
    Je nach Server stehen UID & Co. vor oder hinter dem Literal (iCloud: teils dahinter).
    """
    msgs: list[list] = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            msgs.append([item[0], item[1]])
        elif isinstance(item, bytes):
            if _MSG_START.match(item):
                msgs.append([item, b""])
            elif msgs:
                msgs[-1][0] += b" " + item
    return [(m[0], m[1]) for m in msgs]


# --------------------------------------------------------------------------
# Verbindung
# --------------------------------------------------------------------------

class Imap:
    def __init__(self):
        self.c: imaplib.IMAP4 | None = None
        self.caps: set[str] = set()
        self.selected: tuple[str, bool] | None = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        if not settings.imap_user or not settings.imap_password:
            raise ImapError("IMAP_USER und IMAP_PASSWORD sind nicht gesetzt.")
        try:
            if settings.imap_ssl:
                ctx = ssl.create_default_context()
                self.c = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ctx, timeout=120)
            else:
                self.c = imaplib.IMAP4(settings.imap_host, settings.imap_port, timeout=120)
            self.c.login(settings.imap_user, settings.imap_password)
        except imaplib.IMAP4.error as e:
            raise ImapError(f"IMAP-Login fehlgeschlagen: {e}") from e
        except OSError as e:
            raise ImapError(f"IMAP-Verbindung zu {settings.imap_host}:{settings.imap_port} fehlgeschlagen: {e}") from e
        typ, data = self.c.capability()
        if typ == "OK" and data:
            self.caps = {c.upper() for c in data[0].decode(errors="replace").split()}
        log.info("IMAP verbunden, Capabilities: %s", " ".join(sorted(self.caps)))

    def close(self):
        if self.c is None:
            return
        try:
            if self.selected:
                self.c.close()
        except Exception:
            pass
        try:
            self.c.logout()
        except Exception:
            pass
        self.c = None
        self.selected = None

    # -- Ordner -----------------------------------------------------------

    def list_folders(self) -> list[dict]:
        typ, data = self.c.list()
        if typ != "OK":
            raise ImapError(f"LIST fehlgeschlagen: {data}")
        folders = []
        for item in data or []:
            if isinstance(item, tuple):
                # Name als Literal: (b'(\\HasNoChildren) "/" {12}', b'Ordner Name')
                line = item[0].rsplit(b"{", 1)[0] + b'"' + item[1].replace(b'"', b'\\"') + b'"'
            elif isinstance(item, bytes):
                line = item
            else:
                continue
            m = _LIST_RE.match(line.strip())
            if not m:
                continue
            flags = {f.lower() for f in m.group("flags").decode(errors="replace").split()}
            name = m.group("name").decode(errors="replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if "\\noselect" in flags or "\\nonexistent" in flags:
                continue
            folders.append({"name": name, "display": decode_mutf7(name), "flags": sorted(flags)})
        return folders

    def special_folders(self) -> dict[str, str]:
        """Archiv- und Papierkorb-Ordner bestimmen: Env > SPECIAL-USE > iCloud-Namen."""
        folders = self.list_folders()
        names = {f["name"] for f in folders}
        by_flag = {}
        for f in folders:
            for flag in f["flags"]:
                by_flag.setdefault(flag, f["name"])

        def pick(env_val, flag, candidates):
            if env_val:
                return env_val
            if flag in by_flag:
                return by_flag[flag]
            for c in candidates:
                if c in names:
                    return c
            return ""

        return {
            "archive": pick(settings.archive_folder, "\\archive", ["Archive", "Archiv"]),
            "trash": pick(settings.trash_folder, "\\trash", ["Deleted Messages", "Trash", "Papierkorb"]),
            "junk": pick("", "\\junk", ["Junk", "Spam"]),
        }

    def select(self, folder: str, readonly: bool = True) -> int:
        if self.selected == (folder, readonly):
            return self._uidvalidity
        if self.selected:
            try:
                self.c.close()
            except Exception:
                pass
            self.selected = None
        typ, data = self.c.select(quote_mbox(folder), readonly=readonly)
        if typ != "OK":
            raise ImapError(f"Ordner {folder!r} nicht auswählbar: {data}")
        typ, uv = self.c.response("UIDVALIDITY")
        try:
            self._uidvalidity = int(uv[0]) if uv and uv[0] else 0
        except (TypeError, ValueError):
            self._uidvalidity = 0
        self.selected = (folder, readonly)
        return self._uidvalidity

    def search_all_uids(self) -> list[int]:
        typ, data = self.c.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise ImapError(f"UID SEARCH fehlgeschlagen: {data}")
        out = []
        for line in data or []:
            if line:
                out.extend(int(x) for x in line.split())
        return out

    def fetch_headers(self, uids: list[int]) -> list[HeaderInfo]:
        if not uids:
            return []
        typ, data = self.c.uid("FETCH", uid_set(uids), FETCH_ITEMS)
        if typ != "OK":
            raise ImapError(f"UID FETCH fehlgeschlagen: {data}")
        result = []
        for meta, literal in split_fetch_response(data):
            m = _UID_RE.search(meta)
            if not m:
                continue  # unaufgeforderte FETCH-Antwort (z.B. FLAGS)
            uid = int(m.group(1))
            sm = _SIZE_RE.search(meta)
            size = int(sm.group(1)) if sm else 0
            idate = 0
            tt = imaplib.Internaldate2tuple(meta)
            if tt:
                idate = int(time.mktime(tt))
            try:
                result.append(parse_headers(uid, size, idate, literal))
            except Exception as e:  # einzelne kaputte Mail darf den Sync nicht stoppen
                log.warning("Header von UID %s nicht lesbar: %s", uid, e)
        return result

    def fetch_full(self, folder: str, uid: int) -> bytes:
        self.select(folder, readonly=True)
        for item in ("(BODY.PEEK[])", "(RFC822)"):
            typ, data = self.c.uid("FETCH", str(uid), item)
            if typ != "OK":
                continue
            for meta, literal in split_fetch_response(data):
                if literal:
                    return literal
        raise ImapError(f"Mail UID {uid} in {folder!r} nicht abrufbar")

    def move(self, folder: str, uids: list[int], target: str) -> int:
        """Verschiebt UIDs. Löscht nie endgültig außerhalb des Quellordners."""
        if not uids:
            return 0
        if folder == target:
            return 0
        self.select(folder, readonly=False)
        moved = 0
        for chunk in chunked(sorted(uids), 500):
            s = uid_set(chunk)
            if "MOVE" in self.caps:
                typ, data = self.c.uid("MOVE", s, quote_mbox(target))
                if typ != "OK":
                    raise ImapError(f"UID MOVE nach {target!r} fehlgeschlagen: {data}")
            else:
                typ, data = self.c.uid("COPY", s, quote_mbox(target))
                if typ != "OK":
                    raise ImapError(f"UID COPY nach {target!r} fehlgeschlagen: {data}")
                if "UIDPLUS" not in self.caps:
                    # Ein globales EXPUNGE würde auch fremde \\Deleted-Mails entfernen.
                    raise ImapError("Server kann weder MOVE noch UID EXPUNGE; Verschieben abgebrochen.")
                self.c.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)")
                self.c.uid("EXPUNGE", s)
            moved += len(chunk)
        return moved
