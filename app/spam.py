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


# --------------------------------------------------------------------------
# Anlernen (#12): Bayes in Rspamd, Daten in Redis. Gelernt wird über den Rspamd-Controller,
# der nur im internen Docker-Netz lauscht und ein Passwort verlangt.
# --------------------------------------------------------------------------

def learning_enabled() -> bool:
    return bool(settings.spam_filter and settings.spam_learn and settings.rspamd_password)


def learn(client: httpx.Client, raw: bytes, kind: str) -> str:
    """Lernt eine Mail als 'spam' oder 'ham' an. Gibt 'ok', 'known' (schon so gelernt) oder 'skip'
    (zu wenig Text für Bayes) zurück, wirft SpamError bei allem anderen."""
    url = settings.rspamd_controller_url.rstrip("/") + ("/learnspam" if kind == "spam" else "/learnham")
    try:
        r = client.post(url, content=raw, headers={"Password": settings.rspamd_password})
    except httpx.HTTPError as e:
        raise SpamError(f"Rspamd-Controller unter {settings.rspamd_controller_url} nicht erreichbar: {e}") from None
    if r.status_code == 200:
        return "ok"
    if r.status_code == 208:
        return "known"
    if r.status_code == 204:
        return "skip"
    if r.status_code in (401, 403):
        raise SpamError("Rspamd-Controller lehnt das Passwort ab. RSPAMD_PASSWORD gesetzt und "
                        "Rspamd danach neu gestartet?")
    try:
        detail = r.json().get("error") or r.text
    except ValueError:
        detail = r.text
    raise SpamError(f"Anlernen fehlgeschlagen: HTTP {r.status_code} {detail[:200]}")


def learn_messages(conn: Imap, rows: list[tuple[str, int]], kind: str) -> dict:
    """Holt die Mails (folder, uid) per BODY.PEEK und lernt sie als kind an. Merkt sich das Ergebnis
    in messages.spam_learned und zählt in meta mit. Wirft SpamError, wenn Rspamd nicht mitspielt."""
    counts = {"ok": 0, "known": 0, "skip": 0, "missing": 0}
    if not rows:
        return counts
    other = "ham" if kind == "spam" else "spam"
    by_folder: dict[str, list[int]] = defaultdict(list)
    for folder, uid in rows:
        by_folder[folder].append(uid)
    # Bisheriger Stand: Wird eine Mail umgelernt (Ham -> Spam oder umgekehrt, #16), zählt die alte
    # Klasse eins runter. Rspamd selbst lernt die Mail dabei aus der alten Klasse aus.
    prev: dict[tuple[str, int], str] = {}
    with db.connect() as con:
        for folder, uids in by_folder.items():
            for chunk in chunked(sorted(uids), 900):
                for r in con.execute(
                    f"SELECT uid, spam_learned FROM messages WHERE folder=? AND uid IN ({','.join('?' * len(chunk))})",
                    (folder, *chunk),
                ):
                    prev[(folder, r["uid"])] = r["spam_learned"]
    max_bytes = max(16, settings.spam_max_kb) * 1024
    with _client() as client:
        for folder, uids in by_folder.items():
            for chunk in chunked(uids, FETCH_CHUNK):
                bodies = conn.fetch_partial(folder, chunk, max_bytes)
                marks = []
                for uid in chunk:
                    raw = bodies.get(uid)
                    if raw is None:
                        counts["missing"] += 1
                        continue
                    res = learn(client, raw, kind)
                    counts[res] += 1
                    marks.append((kind if res != "skip" else "skip", folder, uid))
                with db.connect() as con:
                    con.executemany("UPDATE messages SET spam_learned=? WHERE folder=? AND uid=?", marks)
                    db.add_counter(con, f"learned_{kind}", sum(1 for m in marks if m[0] == kind))
                    db.add_counter(con, f"learned_{other}", -sum(
                        1 for m in marks if m[0] == kind and prev.get((m[1], m[2])) == other))
    log.info("Anlernen als %s: %s", kind, counts)
    return counts


def newest(con, by_folder: dict[str, list[int]], limit: int, relearn: str = "") -> list[tuple[str, int]]:
    """Die neuesten `limit` Mails aus by_folder, die noch nicht angelernt sind. Mit relearn='spam'
    bzw. 'ham' auch die, die bisher in der anderen Klasse stehen (Einzelaktion, #16)."""
    cond = "spam_learned NOT IN (?, 'skip')" if relearn else "spam_learned=''"
    extra = (relearn,) if relearn else ()
    rows = []
    for folder, uids in by_folder.items():
        for chunk in chunked(sorted(uids), 900):
            rows += con.execute(
                f"SELECT folder, uid, date FROM messages WHERE folder=? AND {cond} "
                f"AND uid IN ({','.join('?' * len(chunk))})",
                (folder, *extra, *chunk),
            ).fetchall()
    rows.sort(key=lambda r: -r["date"])
    return [(r["folder"], r["uid"]) for r in rows[:limit]]


def ham_pending(con, limit: int) -> list[tuple[str, int]]:
    """Noch nicht angelernte Mails ausgeblendeter Absender aus dem Bewertungszeitraum, neueste zuerst."""
    cutoff = int(time.time()) - settings.spam_max_age_days * 86400
    folders = list(settings.folders) or [""]
    return [(r["folder"], r["uid"]) for r in con.execute(
        f"SELECT folder, uid FROM messages m WHERE spam_learned='' AND date >= ? "
        f"AND folder IN ({','.join('?' * len(folders))}) "
        "AND EXISTS (SELECT 1 FROM sender_state s WHERE s.kept=1 AND "
        "((s.grp='addr' AND s.key=m.from_addr) OR (s.grp='domain' AND s.key=m.org_domain))) "
        "ORDER BY date DESC LIMIT ?",
        (cutoff, *folders, limit),
    )]
