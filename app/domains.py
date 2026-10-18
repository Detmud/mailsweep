"""Grobe Bestimmung der registrierbaren Domain (ohne Netzwerkzugriff auf die Public Suffix List)."""
from __future__ import annotations

# Häufige zweistufige Suffixe. Reicht für das Gruppieren von Absendern.
_MULTI = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.jp", "ne.jp", "or.jp",
    "co.kr", "com.br", "com.tr", "com.cn", "com.hk", "com.sg", "com.mx",
    "com.ar", "co.za", "co.in", "co.il", "com.pl", "com.ua", "co.at",
    "or.at", "gv.at", "com.es", "com.pt", "com.gr", "com.ru", "com.tw",
}


def org_domain(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    parts = domain.split(".")
    if len(parts) <= 2:
        return domain
    if ".".join(parts[-2:]) in _MULTI:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
