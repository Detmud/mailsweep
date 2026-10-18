"""Konfiguration über Umgebungsvariablen; ein Teil davon ist zur Laufzeit in der App änderbar (EDITABLE)."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    """Liest NAME oder, falls gesetzt, den Inhalt der Datei aus NAME_FILE (Docker Secrets)."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path, encoding="utf-8") as fh:
            return fh.read().strip()
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _list(name: str, default: str) -> list[str]:
    raw = _env(name, default) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Settings:
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST", "imap.mail.me.com"))
    imap_port: int = field(default_factory=lambda: int(_env("IMAP_PORT", "993")))
    imap_ssl: bool = field(default_factory=lambda: _bool("IMAP_SSL", True))
    imap_user: str = field(default_factory=lambda: _env("IMAP_USER", "") or "")
    imap_password: str = field(default_factory=lambda: _env("IMAP_PASSWORD", "") or "")

    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "smtp.mail.me.com"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "") or "")
    allow_mailto: bool = field(default_factory=lambda: _bool("ALLOW_MAILTO_UNSUBSCRIBE", True))
    # DNS-Server, über den mailsweep den Abmelde-Host auflöst, wenn der lokale DNS-Filter
    # ihn blockt. Wird nur auf ausdrücklichen Klick genutzt. Leer = Funktion aus.
    unsub_fallback_dns: str = field(default_factory=lambda: _env("UNSUB_FALLBACK_DNS", "9.9.9.9") or "")

    # Ordner, die analysiert werden. "INBOX" oder kommagetrennt, z.B. "INBOX,Archive"
    folders: list[str] = field(default_factory=lambda: _list("SCAN_FOLDERS", "INBOX"))
    # Leer = automatisch per SPECIAL-USE erkennen (iCloud: "Archive", "Deleted Messages")
    archive_folder: str = field(default_factory=lambda: _env("ARCHIVE_FOLDER", "") or "")
    trash_folder: str = field(default_factory=lambda: _env("TRASH_FOLDER", "") or "")

    sync_interval_min: int = field(default_factory=lambda: int(_env("SYNC_INTERVAL_MINUTES", "30")))
    fetch_batch: int = field(default_factory=lambda: int(_env("FETCH_BATCH", "250")))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))

    # authserv-ids, deren Authentication-Results mailsweep vertraut (Suffix-Vergleich, kommagetrennt).
    # iCloud setzt mehrere Header, z. B. dkim-verifier.icloud.com, dmarc.icloud.com, spf.icloud.com.
    trusted_authserv: list[str] = field(default_factory=lambda: [s.lower() for s in _list("TRUSTED_AUTHSERV_IDS", "icloud.com")])

    # Spamfilter (#3): lokaler Rspamd, standardmäßig aus. Bewertet nur Mails der letzten N Tage,
    # pro Sync höchstens spam_per_sync Stück, und holt pro Mail höchstens spam_max_kb KB.
    spam_filter: bool = field(default_factory=lambda: _bool("SPAM_FILTER", False))
    rspamd_url: str = field(default_factory=lambda: _env("RSPAMD_URL", "http://rspamd:11333") or "")
    spam_threshold: int = field(default_factory=lambda: int(_env("SPAM_THRESHOLD", "6")))
    spam_max_age_days: int = field(default_factory=lambda: int(_env("SPAM_MAX_AGE_DAYS", "30")))
    spam_per_sync: int = field(default_factory=lambda: int(_env("SPAM_PER_SYNC", "300")))
    spam_max_kb: int = field(default_factory=lambda: int(_env("SPAM_MAX_KB", "512")))

    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data") or "/data")
    ui_user: str = field(default_factory=lambda: _env("UI_USER", "admin") or "admin")
    ui_password: str = field(default_factory=lambda: _env("UI_PASSWORD", "") or "")
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO") or "INFO")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "mailsweep.sqlite3")

    @property
    def sender_address(self) -> str:
        return self.smtp_from or self.imap_user


settings = Settings()


# Einstellungen, die in der Weboberfläche geändert werden dürfen.
# Zugangsdaten bleiben bewusst ausschließlich in der Env-Datei.
EDITABLE: dict[str, dict] = {
    "dry_run": {"type": "bool", "label": "Testmodus (DRY_RUN)",
                "help": "Nichts verschieben, nichts abmelden, nur protokollieren."},
    "folders": {"type": "list", "label": "Analysierte Ordner",
                "help": "Nur diese Ordner werden ausgewertet. Regeln greifen ebenfalls nur hier."},
    "sync_interval_min": {"type": "int", "min": 0, "max": 1440, "label": "Sync-Intervall (Minuten)",
                          "help": "0 = nur manuell über „Jetzt synchronisieren“."},
    "allow_mailto": {"type": "bool", "label": "Abmelden per Mail erlauben",
                     "help": "Sendet Abmelde-Mails über smtp.mail.me.com."},
    "unsub_fallback_dns": {"type": "str", "validate": "ip", "placeholder": "aus",
                           "label": "Ausweich-DNS für Abmelden",
                           "help": "Wenn dein DNS-Filter eine Abmelde-Domain blockt, kannst du die Abmeldung "
                                   "per Klick über diesen DNS-Server wiederholen. Nur für diesen einen Host, "
                                   "nur auf Klick. Leer = aus."},
    "archive_folder": {"type": "str", "label": "Archiv-Ordner",
                       "help": "Leer = automatisch erkennen (iCloud: Archive)."},
    "trash_folder": {"type": "str", "label": "Papierkorb-Ordner",
                     "help": "Leer = automatisch erkennen (iCloud: Deleted Messages)."},
    "spam_filter": {"type": "bool", "label": "Spamfilter (Rspamd)",
                    "help": "Bewertet neue Mails mit dem lokalen Rspamd. Braucht den Rspamd-Container "
                            "(COMPOSE_PROFILES=spam). Inhalte verlassen das Homelab nicht und werden nicht gespeichert."},
    "spam_threshold": {"type": "int", "min": 1, "max": 50, "label": "Spam-Schwelle (Score)",
                       "help": "Ab diesem Rspamd-Score gilt eine Mail als Spam-Verdacht."},
    "spam_max_age_days": {"type": "int", "min": 1, "max": 3650, "label": "Spam: Mails der letzten Tage",
                          "help": "Nur jüngere Mails werden bewertet. Jede Bewertung lädt die Mail vom Server."},
    "spam_per_sync": {"type": "int", "min": 10, "max": 5000, "label": "Spam: Bewertungen pro Sync",
                      "help": "Begrenzt den IMAP-Verkehr. Der Rest folgt beim nächsten Sync."},
    "fetch_batch": {"type": "int", "min": 10, "max": 1000, "label": "Header pro Abruf",
                    "help": "Bei Timeouts verkleinern."},
}

ENV_DEFAULTS: dict = {k: getattr(settings, k) for k in EDITABLE}


def coerce(key: str, value):
    """Prüft und wandelt einen Wert aus der UI oder der Datenbank in den richtigen Typ."""
    spec = EDITABLE[key]
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}
    if t == "int":
        v = int(value)
        if not spec.get("min", v) <= v <= spec.get("max", v):
            raise ValueError(f"{spec['label']}: erlaubt sind {spec['min']} bis {spec['max']}")
        return v
    if t == "list":
        items = value if isinstance(value, list) else str(value).split(",")
        items = [str(i).strip() for i in items if str(i).strip()]
        if not items:
            raise ValueError(f"{spec['label']}: mindestens ein Ordner")
        return items
    s = str(value or "").strip()
    if s and spec.get("validate") == "ip":
        try:
            ipaddress.ip_address(s)
        except ValueError:
            raise ValueError(f"{spec['label']}: „{s}“ ist keine IP-Adresse") from None
    return s
