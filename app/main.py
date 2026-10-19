"""mailsweep: lokales Abmelde- und Aufräum-Dashboard für IMAP-Postfächer (iCloud)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, jobs, spam, unsubscribe
from . import imap as imap_mod
from .config import EDITABLE, ENV_DEFAULTS, coerce, settings
from .imap import ImapError
from .version import __version__

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mailsweep")

STATIC = os.path.join(os.path.dirname(__file__), "static")


async def _scheduler():
    """Prüft alle 20 s, ob das (in der UI änderbare) Intervall seit dem letzten Sync abgelaufen ist."""
    await asyncio.sleep(3)
    first = True
    while True:
        interval = settings.sync_interval_min
        last = jobs.status.finished or 0
        due = interval > 0 and (first or time.time() - last >= interval * 60)
        if due and not jobs.status.running:
            first = False
            try:
                await asyncio.to_thread(jobs.run_sync)
            except Exception:
                log.exception("Geplanter Sync fehlgeschlagen")
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    applied = db.load_overrides()
    if applied:
        log.info("Einstellungen aus der App: %s", ", ".join(f"{k}={v}" for k, v in applied.items()))
    if not settings.dry_run:
        db.clear_dry_traces()
    log.info("mailsweep %s gestartet", __version__)
    if not settings.ui_password:
        log.warning("UI_PASSWORD ist leer: Die Weboberfläche ist ohne Passwort erreichbar.")
    if settings.dry_run:
        log.warning("DRY_RUN aktiv: Es wird nichts verschoben und nichts abgemeldet.")
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


app = FastAPI(title="mailsweep", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic(auto_error=False)


def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)):
    if settings.ui_password:
        ok = creds is not None and secrets.compare_digest(
            creds.username.encode(), settings.ui_user.encode()
        ) and secrets.compare_digest(creds.password.encode(), settings.ui_password.encode())
        if not ok:
            raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": 'Basic realm="mailsweep"'})
    # CSRF-Schutz: schreibende Requests nur per fetch() mit eigenem Header
    if request.method not in ("GET", "HEAD") and request.headers.get("x-mailsweep") != "1":
        raise HTTPException(403, "Fehlender X-Mailsweep-Header")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return resp


@app.exception_handler(jobs.Busy)
async def busy_handler(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ImapError)
async def imap_handler(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", dependencies=[Depends(auth)], include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/status", dependencies=[Depends(auth)])
def api_status():
    with db.connect() as con:
        total = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM messages").fetchone()
        senders = con.execute("SELECT COUNT(DISTINCT from_addr) FROM messages").fetchone()[0]
        with_unsub = con.execute(
            "SELECT COUNT(DISTINCT from_addr) FROM messages WHERE list_unsub<>''"
        ).fetchone()[0]
        folders = [dict(r) for r in con.execute(
            "SELECT f.name, f.last_sync, (SELECT COUNT(*) FROM messages m WHERE m.folder=f.name) AS count FROM folders f"
        )]
        spam_info = {"enabled": settings.spam_filter, "threshold": settings.spam_threshold,
                     "max_age_days": settings.spam_max_age_days, "error": jobs.status.spam_error}
        if settings.spam_filter:
            cond, params = spam.pending_condition()
            row = con.execute(
                "SELECT COUNT(spam_score), COALESCE(SUM(spam_score >= ?),0) FROM messages", (settings.spam_threshold,)
            ).fetchone()
            spam_info.update(scored=row[0], suspect=row[1],
                             pending=con.execute(f"SELECT COUNT(*) FROM messages WHERE {cond}", params).fetchone()[0])
            spam_info["learn"] = {
                "enabled": spam.learning_enabled(), "configured": bool(settings.rspamd_password),
                "spam": db.get_counter(con, "learned_spam"), "ham": db.get_counter(con, "learned_ham"),
                "min_learns": 200, "error": jobs.status.learn_error,
            }
            spam_info["auto"] = {"enabled": settings.spam_auto_trash, "score": settings.spam_auto_score}
    return {
        "version": __version__,
        "account": settings.imap_user,
        "host": settings.imap_host,
        "scan_folders": settings.folders,
        "dry_run": settings.dry_run,
        "allow_mailto": settings.allow_mailto,
        "sync_interval_min": settings.sync_interval_min,
        "messages": total[0],
        "bytes": total[1],
        "senders": senders,
        "senders_with_unsub": with_unsub,
        "folders": folders,
        "sync": jobs.status.as_dict(),
        "spam": spam_info,
        "now": int(time.time()),
    }


@app.post("/api/sync", dependencies=[Depends(auth)])
async def api_sync():
    if jobs.status.running:
        return {"started": False, "detail": "Sync läuft bereits"}
    asyncio.get_running_loop().run_in_executor(None, jobs.run_sync)
    return {"started": True}


SORTS = {
    "count": "cnt DESC, bytes DESC",
    "size": "bytes DESC, cnt DESC",
    "recent": "last DESC",
    "name": "key ASC",
    "spam": "spam_max DESC, spam_hits DESC, cnt DESC",
}


@app.get("/api/senders", dependencies=[Depends(auth)])
def api_senders(
    group: Literal["addr", "domain"] = "addr",
    q: str = "",
    only_unsub: bool = False,
    only_spam: bool = False,
    show_kept: bool = False,
    show_unsubscribed: bool = True,
    folder: str = "",
    sort: Literal["count", "size", "recent", "name", "spam"] = "count",
    limit: int = 100,
    offset: int = 0,
):
    col = "org_domain" if group == "domain" else "from_addr"
    limit = max(1, min(limit, 500))
    where, params = ["1=1"], []
    if q:
        where.append("(from_addr LIKE ? OR from_name LIKE ? OR org_domain LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if folder:
        where.append("folder = ?")
        params.append(folder)
    having = []
    if only_unsub:
        having.append("has_unsub = 1")
    if only_spam:
        having.append("spam_hits > 0")
    if not show_kept:
        having.append("COALESCE(s.kept,0) = 0")
    if not show_unsubscribed:
        having.append("COALESCE(s.unsubscribed_at,0) = 0")

    inner = f"""
        SELECT {col} AS key,
               COUNT(*) AS cnt,
               SUM(size) AS bytes,
               MAX(date) AS last,
               MIN(date) AS first,
               COUNT(DISTINCT from_addr) AS n_addr,
               GROUP_CONCAT(DISTINCT folder) AS folders,
               MAX(CASE WHEN list_unsub <> '' THEN 1 ELSE 0 END) AS has_unsub,
               MAX(CASE WHEN list_unsub LIKE '%<https:%' AND REPLACE(LOWER(list_unsub_post),' ','') LIKE '%list-unsubscribe=one-click%' THEN 1 ELSE 0 END) AS oneclick,
               MAX(CASE WHEN list_unsub LIKE '%<mailto:%' THEN 1 ELSE 0 END) AS mailto,
               MAX(CASE WHEN list_unsub LIKE '%<http%' THEN 1 ELSE 0 END) AS link,
               SUM(CASE WHEN auth_dkim = 'pass' THEN 1 ELSE 0 END) AS dkim_pass,
               SUM(CASE WHEN auth_dkim = 'pass-unaligned' THEN 1 ELSE 0 END) AS dkim_foreign,
               SUM(CASE WHEN auth_dkim <> '' THEN 1 ELSE 0 END) AS dkim_known,
               MAX(spam_score) AS spam_max,
               SUM(CASE WHEN spam_score >= ? THEN 1 ELSE 0 END) AS spam_hits,
               COUNT(spam_score) AS spam_known
        FROM messages
        WHERE {' AND '.join(where)}
        GROUP BY {col}
    """
    outer = f"""
        SELECT m.*, COALESCE(s.kept,0) AS kept, COALESCE(s.unsubscribed_at,0) AS unsubscribed_at,
               COALESCE(s.unsub_result,'') AS unsub_result,
               (SELECT r.id FROM rules r WHERE r.match_type=? AND r.value=m.key) AS rule_id
        FROM ({inner}) m
        LEFT JOIN sender_state s ON s.grp=? AND s.key=m.key
        {('WHERE ' + ' AND '.join(having)) if having else ''}
    """
    with db.connect() as con:
        full_params = [group, settings.spam_threshold, *params, group]
        total = con.execute(f"SELECT COUNT(*), COALESCE(SUM(cnt),0) FROM ({outer})", full_params).fetchone()
        rows = [dict(r) for r in con.execute(
            f"{outer} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?", [*full_params, limit, offset]
        )]
        keys = [r["key"] for r in rows]
        names: dict[str, str] = {}
        if keys:
            ph = ",".join("?" * len(keys))
            for r in con.execute(
                f"SELECT {col} AS key, from_name FROM messages WHERE {col} IN ({ph}) AND from_name<>'' ORDER BY date",
                keys,
            ):
                names[r["key"]] = r["from_name"]
        moves = _move_stats(con, col, keys)
    for r in rows:
        r["name"] = names.get(r["key"], "")
        r["folders"] = sorted(set((r["folders"] or "").split(","))) if r["folders"] else []
        r["moves"] = moves.get(r["key"])
    return {"total": total[0], "total_messages": total[1], "rows": rows}


def _move_stats(con, col: str, keys: list[str] | None = None) -> dict[str, dict]:
    """Summiert verschobene Mails pro Absender bzw. Domain: {key: {trash, archive, move, dry, last}}."""
    q = (f"SELECT {col} AS key, action, SUM(count) AS n, SUM(bytes) AS b, MAX(dry) AS dry, MAX(ts) AS last "
         f"FROM moves")
    params: list = []
    if keys is not None:
        if not keys:
            return {}
        q += f" WHERE {col} IN ({','.join('?' * len(keys))})"
        params = list(keys)
    q += f" GROUP BY {col}, action"
    out: dict[str, dict] = {}
    for r in con.execute(q, params):
        d = out.setdefault(r["key"], {"trash": 0, "archive": 0, "move": 0, "bytes": 0, "dry": 0, "last": 0})
        d[r["action"]] = d.get(r["action"], 0) + r["n"]
        d["bytes"] += r["b"] or 0
        d["dry"] = max(d["dry"], r["dry"])
        d["last"] = max(d["last"], r["last"])
    return out


@app.get("/api/done", dependencies=[Depends(auth)])
def api_done(group: Literal["addr", "domain"] = "addr", q: str = ""):
    """Alles, was schon bearbeitet wurde: abgemeldet, verschoben oder mit Regel versehen."""
    col = "org_domain" if group == "domain" else "from_addr"
    with db.connect() as con:
        moves = _move_stats(con, col)
        names = {r["key"]: r["name"] for r in con.execute(
            f"SELECT {col} AS key, from_name AS name FROM moves WHERE from_name<>'' ORDER BY ts"
        )}
        unsub: dict[str, dict] = {}
        for r in con.execute(
            "SELECT s.key AS addr, s.unsubscribed_at, s.unsub_result, "
            "COALESCE((SELECT org_domain FROM messages m WHERE m.from_addr=s.key LIMIT 1),"
            " (SELECT org_domain FROM moves v WHERE v.from_addr=s.key LIMIT 1), '') AS org "
            "FROM sender_state s WHERE s.grp='addr' AND s.unsubscribed_at>0"
        ):
            k = r["addr"] if group == "addr" else (r["org"] or r["addr"].rsplit("@", 1)[-1])
            cur = unsub.get(k)
            if not cur or r["unsubscribed_at"] > cur["at"]:
                unsub[k] = {"at": r["unsubscribed_at"], "result": r["unsub_result"]}
        rules = {r["value"]: dict(r) for r in con.execute(
            "SELECT id, value, action, hits, created FROM rules WHERE match_type=?", (group,)
        )}
        keys = set(moves) | set(unsub) | set(rules)
        missing = [k for k in keys if not names.get(k)]
        for i in range(0, len(missing), 500):
            part = missing[i:i + 500]
            for r in con.execute(
                f"SELECT {col} AS key, from_name FROM messages WHERE {col} IN ({','.join('?' * len(part))}) "
                f"AND from_name<>'' ORDER BY date", part,
            ):
                names[r["key"]] = r["from_name"]
        if q:
            ql = q.lower()
            keys = {k for k in keys if ql in k.lower() or ql in names.get(k, "").lower()}
        remaining = {}
        if keys:
            kl = list(keys)
            for i in range(0, len(kl), 500):
                part = kl[i:i + 500]
                for r in con.execute(
                    f"SELECT {col} AS key, COUNT(*) AS n FROM messages WHERE {col} IN ({','.join('?' * len(part))}) GROUP BY {col}",
                    part,
                ):
                    remaining[r["key"]] = r["n"]
    rows = []
    for k in keys:
        m = moves.get(k) or {"trash": 0, "archive": 0, "move": 0, "bytes": 0, "dry": 0, "last": 0}
        u = unsub.get(k)
        rows.append({
            "key": k,
            "name": names.get(k, ""),
            "moves": m,
            "unsubscribed_at": u["at"] if u else 0,
            "unsub_result": u["result"] if u else "",
            "rule": rules.get(k),
            "remaining": remaining.get(k, 0),
            "last": max(m["last"], u["at"] if u else 0, (rules.get(k) or {}).get("created", 0)),
        })
    rows.sort(key=lambda r: r["last"], reverse=True)
    totals = {
        "trash": sum(r["moves"]["trash"] for r in rows if not r["moves"]["dry"]),
        "archive": sum(r["moves"]["archive"] for r in rows if not r["moves"]["dry"]),
        "bytes": sum(r["moves"]["bytes"] for r in rows if not r["moves"]["dry"]),
        "unsubscribed": sum(1 for r in rows if r["unsubscribed_at"]),
    }
    return {"rows": rows, "totals": totals, "dry_run": settings.dry_run}


@app.get("/api/sender", dependencies=[Depends(auth)])
def api_sender(key: str, group: Literal["addr", "domain"] = "addr"):
    col = "org_domain" if group == "domain" else "from_addr"
    with db.connect() as con:
        msgs = [dict(r) for r in con.execute(
            f"SELECT folder, uid, from_addr, subject, date, size, list_id, auth_dkim, auth_dkim_domain, "
            f"spam_score, spam_symbols FROM messages WHERE {col}=? ORDER BY date DESC LIMIT 25",
            (key,),
        )]
        unsub = con.execute(
            f"SELECT list_unsub, list_unsub_post FROM messages WHERE {col}=? AND list_unsub<>'' ORDER BY date DESC LIMIT 1",
            (key,),
        ).fetchone()
        addrs = [dict(r) for r in con.execute(
            f"SELECT from_addr, COUNT(*) AS cnt FROM messages WHERE {col}=? GROUP BY from_addr ORDER BY cnt DESC LIMIT 50",
            (key,),
        )]
        state = con.execute("SELECT * FROM sender_state WHERE grp=? AND key=?", (group, key)).fetchone()
    return {
        "key": key,
        "group": group,
        "messages": msgs,
        "addresses": addrs,
        "list_unsubscribe": dict(unsub) if unsub else None,
        "parsed": unsubscribe.parse_list_unsubscribe(unsub["list_unsub"]) if unsub else None,
        "state": dict(state) if state else None,
    }


AUTH_FIELDS = "AUTHENTICATION-RESULTS ARC-AUTHENTICATION-RESULTS DKIM-SIGNATURE"


@app.get("/api/sender/auth", dependencies=[Depends(auth)])
async def api_sender_auth(key: str, group: Literal["addr", "domain"] = "addr"):
    """Zeigt live vom Server, welche Authentifizierungs-Header die neueste Mail trägt und welche gewertet werden."""
    col = "org_domain" if group == "domain" else "from_addr"
    with db.connect() as con:
        row = con.execute(
            f"SELECT folder, uid, org_domain, auth_dkim, auth_dkim_domain FROM messages WHERE {col}=? "
            f"ORDER BY date DESC LIMIT 1", (key,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Keine Mail dieses Absenders im Cache")

    def load():
        with jobs.imap_job() as conn:
            return conn.fetch_header_fields(row["folder"], row["uid"], AUTH_FIELDS)
    raw = await asyncio.to_thread(load)
    msg = imap_mod.parse_header_block(raw)
    used_name, used = imap_mod.auth_result_headers(msg)
    used_set = set(used)
    lines = []
    for hname in ("Authentication-Results", "ARC-Authentication-Results"):
        for v in msg.get_all(hname) or []:
            v = " ".join(str(v).split())
            aid = imap_mod.authserv_id(v)
            lines.append({"header": hname, "value": v[:1000], "authserv_id": aid,
                          "trusted": imap_mod.is_trusted_authserv(aid),
                          "used": hname == used_name and v in {" ".join(u.split()) for u in used_set}})
    sigs = []
    for v in msg.get_all("DKIM-Signature") or []:
        m = re.search(r"\bd=([^;\s]+)", str(v))
        sigs.append(m.group(1).lower() if m else "?")
    return {"folder": row["folder"], "uid": row["uid"], "auth_dkim": row["auth_dkim"],
            "auth_dkim_domain": row["auth_dkim_domain"], "headers": lines, "dkim_signatures": sigs,
            "trusted_authserv": settings.trusted_authserv}


class SpamTrashReq(BaseModel):
    min_score: float | None = Field(default=None, ge=0, le=1000)
    preview: bool = False


@app.post("/api/spam/trash", dependencies=[Depends(auth)])
async def api_spam_trash(req: SpamTrashReq):
    """Verschiebt alle Mails ab einem Spam-Score in den Papierkorb (ohne ausgeblendete Absender)."""
    if not settings.spam_filter:
        raise HTTPException(400, "Spamfilter ist aus (Einstellungen → Spamfilter).")
    min_score = req.min_score if req.min_score is not None else float(settings.spam_threshold)
    if req.preview:
        return jobs.spam_preview(min_score)
    return await asyncio.to_thread(jobs.spam_trash, min_score)


@app.get("/api/spam/check", dependencies=[Depends(auth)])
async def api_spam_check():
    """Prüft, ob Rspamd erreichbar ist."""
    err = await asyncio.to_thread(spam.ping)
    return {"ok": not err, "detail": err or "Rspamd antwortet", "url": settings.rspamd_url}


class ActionReq(BaseModel):
    group: Literal["addr", "domain"] = "addr"
    keys: list[str] = Field(min_length=1, max_length=2000)
    action: Literal["archive", "trash", "move", "keep", "unkeep", "spam"]
    target_folder: str = ""


@app.post("/api/action", dependencies=[Depends(auth)])
async def api_action(req: ActionReq):
    return await asyncio.to_thread(jobs.sender_action, req.group, req.keys, req.action, req.target_folder)


MSG_SORTS = {
    "recent": "m.date DESC",
    "spam": "m.spam_score IS NULL, m.spam_score DESC, m.date DESC",
    "size": "m.size DESC, m.date DESC",
}
KEPT_SQL = ("EXISTS (SELECT 1 FROM sender_state s WHERE s.kept=1 AND "
            "((s.grp='addr' AND s.key=m.from_addr) OR (s.grp='domain' AND s.key=m.org_domain)))")


@app.get("/api/messages", dependencies=[Depends(auth)])
def api_messages(
    q: str = "",
    folder: str = "",
    only_spam: bool = False,
    show_kept: bool = False,
    sort: Literal["recent", "spam", "size"] = "recent",
    limit: int = 100,
    offset: int = 0,
):
    """Einzelne Mails statt Absender (#16). Nur Header und Score, nie Inhalt."""
    limit = max(1, min(limit, 500))
    where, params = ["1=1"], []
    if q:
        where.append("(m.subject LIKE ? OR m.from_addr LIKE ? OR m.from_name LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if folder:
        where.append("m.folder = ?")
        params.append(folder)
    if only_spam:
        where.append("m.spam_score >= ?")
        params.append(settings.spam_threshold)
    if not show_kept:
        where.append(f"NOT {KEPT_SQL}")
    cond = " AND ".join(where)
    with db.connect() as con:
        total = con.execute(f"SELECT COUNT(*) FROM messages m WHERE {cond}", params).fetchone()[0]
        rows = [dict(r) for r in con.execute(
            f"SELECT m.folder, m.uid, m.from_addr, m.from_name, m.subject, m.date, m.size, m.spam_score, "
            f"m.spam_symbols, m.spam_learned, {KEPT_SQL} AS kept FROM messages m WHERE {cond} "
            f"ORDER BY {MSG_SORTS[sort]} LIMIT ? OFFSET ?",
            [*params, limit, max(0, offset)],
        )]
    return {"total": total, "rows": rows}


class MsgRef(BaseModel):
    folder: str = Field(min_length=1, max_length=500)
    uid: int = Field(ge=1)


class MsgActionReq(BaseModel):
    items: list[MsgRef] = Field(min_length=1, max_length=500)
    action: Literal["spam", "ham", "trash", "archive"]


@app.post("/api/messages/action", dependencies=[Depends(auth)])
async def api_messages_action(req: MsgActionReq):
    if req.action in ("spam", "ham") and not settings.spam_filter:
        raise HTTPException(400, "Spamfilter ist aus (Einstellungen → Spamfilter).")
    try:
        return await asyncio.to_thread(jobs.message_action, [(i.folder, i.uid) for i in req.items], req.action)
    except ValueError as e:
        raise HTTPException(400, str(e))


class UnsubReq(BaseModel):
    addr: str
    method: Literal["auto", "oneclick", "mailto"] = "auto"
    dns: Literal["system", "fallback"] = "system"


@app.post("/api/unsubscribe", dependencies=[Depends(auth)])
async def api_unsubscribe(req: UnsubReq):
    return await asyncio.to_thread(unsubscribe.unsubscribe, req.addr.strip().lower(), req.method, req.dns)


class MarkReq(BaseModel):
    addr: str
    done: bool = True


@app.post("/api/unsubscribe/mark", dependencies=[Depends(auth)])
def api_unsub_mark(req: MarkReq):
    with db.connect() as con:
        con.execute(
            "INSERT INTO sender_state(grp, key, unsubscribed_at, unsub_result) VALUES ('addr',?,?,?) "
            "ON CONFLICT(grp, key) DO UPDATE SET unsubscribed_at=excluded.unsubscribed_at, unsub_result=excluded.unsub_result",
            (req.addr, int(time.time()) if req.done else 0, "manuell markiert" if req.done else ""),
        )
    db.add_log("unsubscribe", req.addr, "manuell als abgemeldet markiert" if req.done else "Markierung entfernt")
    return {"ok": True}


@app.get("/api/folders", dependencies=[Depends(auth)])
async def api_folders():
    if not jobs.status.folders:
        def load():
            with jobs.imap_job() as conn:
                jobs.status.folders = conn.list_folders()
                jobs.status.special = conn.special_folders()
        await asyncio.to_thread(load)
    return {"folders": jobs.status.folders, "special": jobs.status.special}


class RuleReq(BaseModel):
    match_type: Literal["addr", "domain"]
    value: str = Field(min_length=1, max_length=300)
    action: Literal["archive", "trash", "move"]
    target_folder: str = ""
    apply_now: bool = True


@app.get("/api/rules", dependencies=[Depends(auth)])
def api_rules():
    with db.connect() as con:
        return {"rules": [dict(r) for r in con.execute("SELECT * FROM rules ORDER BY created DESC")]}


@app.post("/api/rules", dependencies=[Depends(auth)])
async def api_rule_create(req: RuleReq):
    if req.action == "move" and not req.target_folder:
        raise HTTPException(400, "Zielordner fehlt")
    value = req.value.strip().lower()
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO rules(match_type, value, action, target_folder, created) VALUES (?,?,?,?,?) "
            "ON CONFLICT(match_type, value) DO UPDATE SET action=excluded.action, target_folder=excluded.target_folder "
            "RETURNING id",
            (req.match_type, value, req.action, req.target_folder, int(time.time())),
        )
        rule_id = cur.fetchone()[0]
    db.add_log("rule-create", f"#{rule_id} {value}", f"{req.action} {req.target_folder}".strip())
    moved = 0
    if req.apply_now:
        moved = await asyncio.to_thread(jobs.apply_rule_now, rule_id)
    return {"id": rule_id, "moved": moved}


@app.delete("/api/rules/{rule_id}", dependencies=[Depends(auth)])
def api_rule_delete(rule_id: int):
    with db.connect() as con:
        con.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    db.add_log("rule-delete", f"#{rule_id}")
    return {"ok": True}


@app.get("/api/log", dependencies=[Depends(auth)])
def api_log(limit: int = 200):
    with db.connect() as con:
        return {"log": [dict(r) for r in con.execute(
            "SELECT * FROM log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
        )]}


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

def _settings_payload():
    with db.connect() as con:
        stored = {r["key"] for r in con.execute("SELECT key FROM app_settings")}
    items = []
    for key, spec in EDITABLE.items():
        items.append({
            "key": key,
            **spec,
            "value": getattr(settings, key),
            "env_value": ENV_DEFAULTS[key],
            "source": "app" if key in stored else "env",
        })
    return {"version": __version__, "settings": items, "folders": jobs.status.folders}


@app.get("/api/settings", dependencies=[Depends(auth)])
def api_settings():
    return _settings_payload()


class SettingsReq(BaseModel):
    values: dict


@app.post("/api/settings", dependencies=[Depends(auth)])
async def api_settings_save(req: SettingsReq):
    changed = {}
    new_values = {}
    for key, raw in req.values.items():
        if key not in EDITABLE:
            raise HTTPException(400, f"Unbekannte Einstellung {key}")
        try:
            new_values[key] = coerce(key, raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e))
    for key, val in new_values.items():
        old = getattr(settings, key)
        if val == old:
            continue
        setattr(settings, key, val)
        db.save_override(key, val)
        changed[key] = (old, val)
    if changed:
        db.add_log("settings", ", ".join(changed), "; ".join(f"{k}: {o} → {n}" for k, (o, n) in changed.items()))
    if "dry_run" in changed and not settings.dry_run:
        db.clear_dry_traces()
        log.warning("DRY_RUN ausgeschaltet: Aktionen werden ab jetzt wirklich ausgeführt.")
    if {"spam_filter", "spam_learn"} & changed.keys():
        jobs.status.learn_error = ""
    if "spam_filter" in changed:
        jobs.status.spam_error = ""
        if settings.spam_filter and not jobs.status.running:
            asyncio.get_running_loop().run_in_executor(None, jobs.run_sync)
    if {"archive_folder", "trash_folder"} & changed.keys():
        jobs.status.special = {}
    if "folders" in changed:
        # Entfernte Ordner sofort aus Liste und Zählern nehmen, nicht erst nach dem nächsten Sync
        removed = db.prune_folders(settings.folders)
        if removed:
            db.add_log("settings", "folders", f"{removed} Einträge aus entfernten Ordnern verworfen")
        if not jobs.status.running:
            asyncio.get_running_loop().run_in_executor(None, jobs.run_sync)
    return {"changed": list(changed), **_settings_payload()}


@app.delete("/api/settings/{key}", dependencies=[Depends(auth)])
def api_settings_reset(key: str):
    if key not in EDITABLE:
        raise HTTPException(404, "Unbekannte Einstellung")
    db.delete_override(key)
    old = getattr(settings, key)
    setattr(settings, key, ENV_DEFAULTS[key])
    db.add_log("settings", key, f"auf Env-Wert zurückgesetzt: {old} → {ENV_DEFAULTS[key]}")
    if key == "dry_run" and not settings.dry_run:
        db.clear_dry_traces()
    if key == "folders" and old != settings.folders:
        db.prune_folders(settings.folders)
        if not jobs.status.running:
            threading.Thread(target=jobs.run_sync, daemon=True).start()
    return _settings_payload()
