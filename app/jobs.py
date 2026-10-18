"""Sync, Aufräum-Aktionen und Regeln. Alle IMAP-Jobs laufen seriell unter einem Lock."""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

from . import db
from .config import settings
from .imap import Imap, ImapError, chunked

log = logging.getLogger("mailsweep.jobs")

imap_lock = threading.Lock()


class Busy(RuntimeError):
    pass


@contextmanager
def imap_job(wait: float = 5.0):
    if not imap_lock.acquire(timeout=wait):
        raise Busy("Gerade läuft ein Sync oder eine andere Aktion. Bitte kurz warten.")
    try:
        with Imap() as conn:
            yield conn
    finally:
        imap_lock.release()


# --------------------------------------------------------------------------
# Status für die UI
# --------------------------------------------------------------------------

class SyncStatus:
    def __init__(self):
        self.running = False
        self.phase = ""
        self.folder = ""
        self.done = 0
        self.total = 0
        self.started = 0
        self.finished = 0
        self.error = ""
        self.special: dict[str, str] = {}
        self.folders: list[dict] = []

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items()}


status = SyncStatus()


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def run_sync() -> None:
    """Synchronisiert Header-Metadaten aller SCAN_FOLDERS und wendet danach die Regeln an."""
    if status.running:
        return
    status.running = True
    status.error = ""
    status.started = int(time.time())
    status.done = status.total = 0
    try:
        with imap_job(wait=600) as conn:
            status.phase = "Ordner lesen"
            status.folders = conn.list_folders()
            status.special = conn.special_folders()
            existing = {f["name"] for f in status.folders}
            # Die Ordnerliste kann sich während des Syncs in den Einstellungen ändern:
            # neu hinzugekommene Ordner werden noch in diesem Lauf eingelesen,
            # entfernte nicht mehr angefasst.
            seen: set[str] = set()
            while todo := [f for f in settings.folders if f not in seen]:
                for folder in todo:
                    seen.add(folder)
                    if folder not in settings.folders:
                        continue
                    if folder not in existing:
                        log.warning("Ordner %r existiert nicht, übersprungen", folder)
                        db.add_log("sync", folder, "Ordner existiert nicht", ok=False)
                        continue
                    _sync_folder(conn, folder)
            removed = db.prune_folders(set(settings.folders) & existing)
            if removed:
                log.info("%d Einträge aus nicht mehr analysierten Ordnern entfernt", removed)
            status.phase = "Regeln anwenden"
            _apply_rules(conn)
        status.phase = "fertig"
    except Busy:
        status.phase = "übersprungen (beschäftigt)"
    except Exception as e:
        log.exception("Sync fehlgeschlagen")
        status.error = str(e)
        status.phase = "Fehler"
        db.add_log("sync", "-", str(e), ok=False)
    finally:
        status.running = False
        status.finished = int(time.time())


def _sync_folder(conn: Imap, folder: str) -> None:
    status.folder = folder
    status.phase = "UIDs abgleichen"
    uidvalidity = conn.select(folder, readonly=True)
    with db.connect() as con:
        row = con.execute("SELECT uidvalidity FROM folders WHERE name=?", (folder,)).fetchone()
        if row and row["uidvalidity"] != uidvalidity:
            log.info("UIDVALIDITY von %s geändert, Ordner wird neu eingelesen", folder)
            con.execute("DELETE FROM messages WHERE folder=?", (folder,))
        con.execute(
            "INSERT INTO folders(name, uidvalidity, last_sync) VALUES (?,?,0) "
            "ON CONFLICT(name) DO UPDATE SET uidvalidity=excluded.uidvalidity",
            (folder, uidvalidity),
        )
        known = {r[0] for r in con.execute("SELECT uid FROM messages WHERE folder=?", (folder,))}

    server = set(conn.search_all_uids())
    gone = known - server
    new = sorted(server - known)
    if gone:
        with db.connect() as con:
            for chunk in chunked(sorted(gone), 900):
                con.execute(
                    f"DELETE FROM messages WHERE folder=? AND uid IN ({','.join('?' * len(chunk))})",
                    (folder, *chunk),
                )
    status.phase = "Header laden"
    status.total += len(new)
    log.info("%s: %d auf dem Server, %d neu, %d entfernt", folder, len(server), len(new), len(gone))

    # Neueste zuerst, damit die UI schnell sinnvolle Daten zeigt
    for chunk in chunked(list(reversed(new)), settings.fetch_batch):
        infos = conn.fetch_headers(chunk)
        with db.connect() as con:
            con.executemany(
                "INSERT OR REPLACE INTO messages(folder, uid, from_addr, from_name, from_domain, org_domain,"
                " subject, date, size, list_unsub, list_unsub_post, list_id, auth_dkim, auth_dkim_domain)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (folder, h.uid, h.from_addr, h.from_name, h.from_domain, h.org_domain, h.subject,
                     h.date, h.size, h.list_unsub, h.list_unsub_post, h.list_id, h.auth_dkim,
                     h.auth_dkim_domain)
                    for h in infos
                ],
            )
        status.done += len(chunk)

    with db.connect() as con:
        con.execute("UPDATE folders SET last_sync=? WHERE name=?", (int(time.time()), folder))


# --------------------------------------------------------------------------
# Aktionen
# --------------------------------------------------------------------------

def _key_col(group: str) -> str:
    return "org_domain" if group == "domain" else "from_addr"


def _locations(con, group: str, keys: list[str], folder_filter: list[str] | None = None):
    col = _key_col(group)
    by_folder: dict[str, list[int]] = defaultdict(list)
    for chunk in chunked(keys, 500):
        q = f"SELECT folder, uid FROM messages WHERE {col} IN ({','.join('?' * len(chunk))})"
        params = list(chunk)
        if folder_filter:
            q += f" AND folder IN ({','.join('?' * len(folder_filter))})"
            params += folder_filter
        for r in con.execute(q, params):
            by_folder[r["folder"]].append(r["uid"])
    return by_folder


def resolve_target(action: str, target_folder: str = "") -> str:
    if action == "archive":
        t = status.special.get("archive") or settings.archive_folder
        if not t:
            raise ImapError("Kein Archiv-Ordner gefunden. ARCHIVE_FOLDER setzen.")
        return t
    if action == "trash":
        t = status.special.get("trash") or settings.trash_folder
        if not t:
            raise ImapError("Kein Papierkorb gefunden. TRASH_FOLDER setzen.")
        return t
    if action == "move":
        if not target_folder:
            raise ImapError("Zielordner fehlt.")
        return target_folder
    raise ImapError(f"Unbekannte Aktion {action!r}")


def _ensure_special(conn: Imap):
    if not status.special:
        status.folders = conn.list_folders()
        status.special = conn.special_folders()


def _record_moves(con, folder: str, uids: list[int], action: str, target: str, source: str) -> None:
    """Merkt sich pro Absender, wie viele Mails wohin verschoben wurden (für Status und Tab „Erledigt“)."""
    agg: dict[str, list] = {}
    for chunk in chunked(sorted(uids), 900):
        for r in con.execute(
            f"SELECT from_addr, org_domain, MAX(from_name) AS name, COUNT(*) AS n, SUM(size) AS b FROM messages "
            f"WHERE folder=? AND uid IN ({','.join('?' * len(chunk))}) GROUP BY from_addr, org_domain",
            (folder, *chunk),
        ):
            a = agg.setdefault(r["from_addr"], [r["org_domain"], r["name"] or "", 0, 0])
            a[2] += r["n"]
            a[3] += r["b"] or 0
    dry = 1 if settings.dry_run else 0
    now = int(time.time())
    for addr, (org, name, n, b) in agg.items():
        if dry:
            # Testmodus: nur den letzten Stand pro Absender und Aktion behalten
            con.execute("DELETE FROM moves WHERE dry=1 AND from_addr=? AND action=?", (addr, action))
        con.execute(
            "INSERT INTO moves(ts, from_addr, org_domain, from_name, action, target, count, bytes, source, dry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, addr, org, name, action, target, n, b, source, dry),
        )


def move_messages(conn: Imap, by_folder: dict[str, list[int]], target: str,
                  action: str = "move", source: str = "manual") -> int:
    total = 0
    for folder, uids in by_folder.items():
        if folder == target or not uids:
            continue
        if settings.dry_run:
            total += len(uids)
            if source == "manual":
                with db.connect() as con:
                    _record_moves(con, folder, uids, action, target, source)
            continue
        moved = conn.move(folder, uids, target)
        total += moved
        with db.connect() as con:
            _record_moves(con, folder, uids, action, target, source)
            for chunk in chunked(sorted(uids), 900):
                con.execute(
                    f"DELETE FROM messages WHERE folder=? AND uid IN ({','.join('?' * len(chunk))})",
                    (folder, *chunk),
                )
    return total


def sender_action(group: str, keys: list[str], action: str, target_folder: str = "") -> dict:
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return {"moved": 0}
    if action in ("keep", "unkeep"):
        with db.connect() as con:
            for k in keys:
                con.execute(
                    "INSERT INTO sender_state(grp, key, kept) VALUES (?,?,?) "
                    "ON CONFLICT(grp, key) DO UPDATE SET kept=excluded.kept",
                    (group, k, 1 if action == "keep" else 0),
                )
        db.add_log(action, ", ".join(keys)[:500], f"{len(keys)} Absender")
        return {"moved": 0}

    with imap_job() as conn:
        _ensure_special(conn)
        target = resolve_target(action, target_folder)
        with db.connect() as con:
            by_folder = _locations(con, group, keys)
        try:
            moved = move_messages(conn, by_folder, target, action, "manual")
        except Exception as e:
            db.add_log(action, ", ".join(keys)[:500], str(e), ok=False)
            raise
    prefix = "[DRY-RUN] " if settings.dry_run else ""
    db.add_log(action, ", ".join(keys)[:500], f"{prefix}{moved} Mails nach {target!r}")
    return {"moved": moved, "target": target, "dry_run": settings.dry_run}


# --------------------------------------------------------------------------
# Regeln
# --------------------------------------------------------------------------

def _apply_rules(conn: Imap, only_rule_id: int | None = None) -> int:
    with db.connect() as con:
        q = "SELECT * FROM rules"
        params: tuple = ()
        if only_rule_id is not None:
            q += " WHERE id=?"
            params = (only_rule_id,)
        rules = [dict(r) for r in con.execute(q, params)]
    if not rules:
        return 0
    _ensure_special(conn)
    total = 0
    for rule in rules:
        try:
            target = resolve_target(rule["action"], rule["target_folder"])
        except ImapError as e:
            db.add_log("rule", f"#{rule['id']} {rule['value']}", str(e), ok=False)
            continue
        scan = [f for f in settings.folders if f != target]
        with db.connect() as con:
            by_folder = _locations(con, rule["match_type"], [rule["value"]], scan)
        n = sum(len(v) for v in by_folder.values())
        if not n:
            continue
        try:
            moved = move_messages(conn, by_folder, target, rule["action"], "rule")
        except Exception as e:
            db.add_log("rule", f"#{rule['id']} {rule['value']}", str(e), ok=False)
            continue
        total += moved
        with db.connect() as con:
            con.execute("UPDATE rules SET hits = hits + ? WHERE id=?", (moved, rule["id"]))
        prefix = "[DRY-RUN] " if settings.dry_run else ""
        db.add_log("rule", f"#{rule['id']} {rule['value']}", f"{prefix}{moved} Mails nach {target!r}")
    return total


def apply_rule_now(rule_id: int) -> int:
    with imap_job() as conn:
        return _apply_rules(conn, only_rule_id=rule_id)
