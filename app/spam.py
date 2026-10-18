"""Spam-Bewertung über einen lokalen Rspamd (#3).

Pro Mail werden die ersten SPAM_MAX_KB per BODY.PEEK geholt (setzt kein Gelesen-Flag), an Rspamd
geschickt und nur Score und Regelnamen gespeichert. Der Mailinhalt landet nie in der Datenbank.
Rspamd läuft im eigenen Container ohne Internetzugang, siehe compose.yaml und rspamd/.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

import httpx

from . import db
from .config import settings
from .imap import Imap, chunked

log = logging.getLogger("mailsweep.spam")

FETCH_CHUNK = 20  # Mails pro IMAP-Abruf


class SpamError(RuntimeError):
    pass


def _client() -> httpx.Client:
    # trust_env=False: keine Proxy-Variablen aus der Umgebung, Rspamd ist nur intern erreichbar
    return httpx.Client(trust_env=False, timeout=httpx.Timeout(30.0, connect=5.0))


def check(client: httpx.Client, raw: bytes) -> tuple[float, str]:
    """Lässt eine Mail von Rspamd bewerten. Gibt (Score, wichtigste Regeln) zurück."""
    url = settings.rspamd_url.rstrip("/") + "/checkv2"
    try:
        r = client.post(url, content=raw)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SpamError(f"Rspamd unter {settings.rspamd_url} nicht erreichbar: {e}") from None
    score = float(data.get("score") or 0)
    syms = sorted(
        ((float(v.get("score") or 0), k) for k, v in (data.get("symbols") or {}).items() if v.get("score")),
        key=lambda x: -abs(x[0]),
    )
    text = " ".join(f"{k}({s:+.1f})" for s, k in syms[:12])
    return round(score, 2), text[:500]


def ping() -> str:
    """Leer, wenn Rspamd antwortet, sonst die Fehlermeldung."""
    try:
        with _client() as c:
            r = c.get(settings.rspamd_url.rstrip("/") + "/ping", timeout=3)
            return "" if r.status_code == 200 else f"Rspamd antwortet mit HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return f"Rspamd unter {settings.rspamd_url} nicht erreichbar: {e}"


def pending_condition() -> tuple[str, list]:
    """SQL-Bedingung für Mails, die noch bewertet werden sollen."""
    cutoff = int(time.time()) - settings.spam_max_age_days * 86400
    folders = list(settings.folders) or [""]
    return (f"spam_score IS NULL AND date >= ? AND folder IN ({','.join('?' * len(folders))})",
            [cutoff, *folders])


def score_pending(conn: Imap, progress=None) -> int:
    """Bewertet bis zu SPAM_PER_SYNC unbewertete Mails, neueste zuerst. Gibt die Anzahl zurück."""
    if not settings.spam_filter:
        return 0
    cond, params = pending_condition()
    with db.connect() as con:
        rows = con.execute(
            f"SELECT folder, uid FROM messages WHERE {cond} ORDER BY date DESC LIMIT ?",
            (*params, settings.spam_per_sync),
        ).fetchall()
    if not rows:
        return 0
    by_folder: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_folder[r["folder"]].append(r["uid"])
    if progress:
        progress(0, len(rows))
    done = 0
    max_bytes = max(16, settings.spam_max_kb) * 1024
    with _client() as client:
        for folder, uids in by_folder.items():
            for chunk in chunked(uids, FETCH_CHUNK):
                bodies = conn.fetch_partial(folder, chunk, max_bytes)
                results = []
                for uid in chunk:
                    raw = bodies.get(uid)
                    if raw is None:
                        continue  # inzwischen gelöscht oder verschoben, der nächste Sync räumt auf
                    score, symbols = check(client, raw)
                    results.append((score, symbols, folder, uid))
                with db.connect() as con:
                    con.executemany(
                        "UPDATE messages SET spam_score=?, spam_symbols=? WHERE folder=? AND uid=?", results
                    )
                done += len(chunk)
                if progress:
                    progress(done, len(rows))
    log.info("Spamfilter: %d Mails bewertet", done)
    return done
