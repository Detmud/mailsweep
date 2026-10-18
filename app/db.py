"""SQLite-Speicher. Es werden nur Header-Metadaten gespeichert, nie Mail-Inhalte."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    folder          TEXT    NOT NULL,
    uid             INTEGER NOT NULL,
    from_addr       TEXT    NOT NULL DEFAULT '',
    from_name       TEXT    NOT NULL DEFAULT '',
    from_domain     TEXT    NOT NULL DEFAULT '',
    org_domain      TEXT    NOT NULL DEFAULT '',
    subject         TEXT    NOT NULL DEFAULT '',
    date            INTEGER NOT NULL DEFAULT 0,
    size            INTEGER NOT NULL DEFAULT 0,
    list_unsub      TEXT    NOT NULL DEFAULT '',
    list_unsub_post TEXT    NOT NULL DEFAULT '',
    list_id         TEXT    NOT NULL DEFAULT '',
    auth_dkim       TEXT    NOT NULL DEFAULT '',
    auth_dkim_domain TEXT   NOT NULL DEFAULT '',
    PRIMARY KEY (folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_msg_org ON messages(org_domain);
CREATE INDEX IF NOT EXISTS idx_msg_date ON messages(date);

CREATE TABLE IF NOT EXISTS folders (
    name        TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_sync   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sender_state (
    grp             TEXT NOT NULL,          -- 'addr' oder 'domain'
    key             TEXT NOT NULL,
    kept            INTEGER NOT NULL DEFAULT 0,
    unsubscribed_at INTEGER NOT NULL DEFAULT 0,
    unsub_result    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (grp, key)
);

CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type    TEXT NOT NULL,            -- 'addr' oder 'domain'
    value         TEXT NOT NULL,
    action        TEXT NOT NULL,            -- 'archive' | 'trash' | 'move'
    target_folder TEXT NOT NULL DEFAULT '',
    created       INTEGER NOT NULL,
    hits          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (match_type, value)
);

CREATE TABLE IF NOT EXISTS moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    from_addr  TEXT NOT NULL,
    org_domain TEXT NOT NULL,
    from_name  TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,               -- 'archive' | 'trash' | 'move'
    target     TEXT NOT NULL,
    count      INTEGER NOT NULL,
    bytes      INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'manual', -- 'manual' | 'rule'
    dry        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_moves_addr ON moves(from_addr);
CREATE INDEX IF NOT EXISTS idx_moves_org ON moves(org_domain);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    ok      INTEGER NOT NULL DEFAULT 1
);
"""


SCHEMA_VERSION = 2


def init_db() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        current = int(row[0]) if row else 1
        if current < 2:
            # v2 liest zusätzlich ARC-Authentication-Results: Header-Cache einmal neu aufbauen
            con.execute("DELETE FROM messages")
            con.execute("DELETE FROM folders")
        con.execute(
            "INSERT INTO meta(key, value) VALUES ('schema', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def clear_dry_traces() -> None:
    """Entfernt Spuren aus dem Testmodus, damit nichts als erledigt gilt, was nie passiert ist."""
    with connect() as con:
        con.execute("DELETE FROM moves WHERE dry=1")
        con.execute(
            "UPDATE sender_state SET unsubscribed_at=0, unsub_result='' WHERE unsub_result LIKE '[DRY-RUN]%'"
        )


def load_overrides() -> dict:
    """Wendet in der App gespeicherte Einstellungen auf das settings-Objekt an (überschreiben die Env)."""
    import json
    from .config import EDITABLE, coerce
    applied = {}
    with connect() as con:
        for r in con.execute("SELECT key, value FROM app_settings"):
            if r["key"] not in EDITABLE:
                continue
            try:
                val = coerce(r["key"], json.loads(r["value"]))
            except Exception:
                continue
            setattr(settings, r["key"], val)
            applied[r["key"]] = val
    return applied


def save_override(key: str, value) -> None:
    import json
    with connect() as con:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def delete_override(key: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM app_settings WHERE key=?", (key,))


def prune_folders(keep) -> int:
    """Entfernt Header-Daten aller Ordner, die nicht in keep stehen (nicht mehr analysiert oder
    auf dem Server verschwunden). Verlauf (moves, log), Regeln und Absender-Status bleiben."""
    keep = list(keep)
    ph = ",".join("?" * len(keep))
    cond = f"NOT IN ({ph})" if keep else "IS NOT NULL"
    with connect() as con:
        n = con.execute(f"DELETE FROM messages WHERE folder {cond}", keep).rowcount
        con.execute(f"DELETE FROM folders WHERE name {cond}", keep)
    return n


@contextmanager
def connect():
    con = sqlite3.connect(settings.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def add_log(action: str, target: str, detail: str = "", ok: bool = True) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO log(ts, action, target, detail, ok) VALUES (?,?,?,?,?)",
            (int(time.time()), action, target, detail[:2000], 1 if ok else 0),
        )
