# mailsweep: Rspamd bewertet nur lokal. Kein DNS nach außen: Abfragen gehen an 127.0.0.1,
# dort lauscht nichts, sie scheitern sofort. Zusätzlich hängt der Container in einem
# Docker-Netz ohne Internetzugang (compose.yaml, Netz "spam").
dns {
  nameserver = ["127.0.0.1:53"];
  timeout = 0.5s;
  retransmits = 1;
}
# Keine Update- und Versionsabfragen
disable_monitoring = true;
local_addrs = [];
