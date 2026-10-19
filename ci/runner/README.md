# Forgejo-Runner für mailsweep (#11)

`.forgejo/workflows/test.yml` startet bei jedem Pull Request und jedem Push auf `main` einen Job, der `tests/ci.sh` in einem Container `ubuntu:24.04` ausführt. Dafür braucht Forgejo einen Runner mit dem Label `docker`. Diese Datei beschreibt, wo er laufen sollte, wie er eingerichtet wird und wie danach der Branch-Schutz greift.

## Was der Job tut

1. `apt-get install` von git, Python 3.12, Dovecot und Rspamd 3.8 (Ubuntu-Pakete, wie im Rspamd-Image).
2. Checkout per `git fetch` mit dem Job-Token. Keine fremden Actions, also kein Download von `actions/checkout` aus dem Internet.
3. `tests/ci.sh`: `ruff`, `test_parse.py`, `test_net.py`, eigener Dovecot auf 127.0.0.1:10143 mit frischem Postfach, `seed.py`, `test_e2e.py`, `test_spam.py` mit Fake-Rspamd und noch einmal gegen einen echten Rspamd mit `rspamd/`.

Der Job braucht Internet für die Ubuntu-Paketquellen und PyPI. Er braucht **keine** Zugangsdaten zu iCloud und sieht keine echten Mails. Laufzeit etwa 2 bis 4 Minuten, der Großteil ist `apt-get`.

## Wo der Runner läuft: Sicherheitsabwägung

Ein Runner führt Code aus dem Repository aus. Wichtiger ist aber: Er steuert Docker. Wer den Docker-Socket hat, hat faktisch root auf dem Host, auf dem dieser Docker läuft. Auf der Komodo-VM wären das alle Stacks, ihre Volumes und die Secrets in `/etc/komodo`.

| Option | Aufwand | Was ein kompromittierter Runner erreicht |
|---|---|---|
| **A. Eigener LXC nur für CI** (empfohlen) | neuer unprivilegierter LXC mit Docker | root im LXC. Der ist unprivilegiert, also auf dem Proxmox-Host ein normaler Benutzer. Komodo-VM und Stacks bleiben unberührt. |
| B. Docker-in-Docker auf der Komodo-VM | zweiter Container `docker:dind` | Der DinD-Container muss `privileged: true` laufen. Ausbruch daraus ist trivial, also wieder root auf der VM. Kaum besser als der Socket. |
| C. Rootless Docker auf der Komodo-VM | eigener Benutzer mit rootless Docker | Der Benutzer, nicht root. Trennung hängt aber an Kernel-Namespaces auf derselben VM, und `apt-get` in Jobs braucht Anpassungen. |
| D. Socket der Komodo-VM einhängen | am einfachsten | root auf der VM. Nicht empfohlen. |

Empfehlung: **Option A**. Wer Pull Requests anlegen kann, kann Code im Runner ausführen. Heute sind das nur Sebastian und Claude, aber die Trennung kostet wenig und hält die Komodo-VM sauber.

Auch bei Option A bleibt der LXC im LAN. Wer es enger will, beschränkt ihn per Proxmox-Firewall auf Forgejo (192.168.10.15:443 bzw. :3300), DNS und ausgehend HTTPS für Paketquellen und PyPI.

Die Entscheidung trifft Sebastian. Die Dateien in diesem Ordner passen zu Option A, gehen aber mit angepasstem Pfad auch für C.

## Einrichtung (Option A)

1. **LXC anlegen** auf Proxmox: Ubuntu 24.04, unprivilegiert, Features `nesting=1` und `keyctl=1` (für Docker im LXC), 2 CPU, 2 GB RAM, 16 GB Platte. Docker installieren (`apt install docker.io docker-compose-v2`).
2. **Runner in Forgejo anlegen:** Repo `home.arpa/mailsweep` → Einstellungen → Actions → Runner → „Neuen Runner erstellen“. Name `mailsweep-ci`. UUID und Token kopieren. Das Token erscheint nur einmal.
3. **Konfiguration** im LXC:
   ```bash
   mkdir -p ~/forgejo-runner/data && cd ~/forgejo-runner
   # compose.yaml und config.example.yml aus diesem Ordner hierher kopieren
   cp config.example.yml data/config.yml
   nano data/config.yml        # uuid und token eintragen
   chown -R 1000:1000 data && chmod 600 data/config.yml
   DOCKER_GID=$(stat -c %g /var/run/docker.sock) docker compose up -d
   docker logs -f forgejo-runner   # "runner ... declared successfully" o. ä.
   ```
   Alternativ als Komodo-Stack auf dem LXC, wenn dort ein Komodo-Periphery läuft. UUID und Token dann als Komodo-Secret.
4. In Forgejo sollte der Runner jetzt unter Einstellungen → Actions → Runner als „Idle“ erscheinen.
5. **Probelauf:** einen beliebigen Pull Request öffnen oder unter Actions den letzten Lauf neu starten.

## Erreichbarkeit von Forgejo aus dem Job

Der Checkout holt den Code von `https://git.home.arpa/` (der `ROOT_URL` von Forgejo). Der Job-Container muss diesen Namen auflösen und dem Zertifikat vertrauen.

- Klappt das nicht (Fehler `Could not resolve host` oder `SSL certificate problem`), eine **Repo-Variable** `CI_GIT_URL` setzen: Repo → Einstellungen → Actions → Variablen. Zum Beispiel `http://192.168.10.15:3300`. Dann geht das Job-Token unverschlüsselt durchs LAN. Es gilt nur für die Dauer des Jobs und nur für dieses Repo.
- Sauberer: die interne CA im LXC hinterlegen und per `container.options` und `valid_volumes` in den Job einhängen. Das lohnt sich erst, wenn mehrere Repos CI nutzen.

## Branch-Schutz danach

Wenn ein paar Läufe grün waren: Repo → Einstellungen → Branches → Schutzregel für `main`:

- Push nur per Pull Request.
- Statusprüfung verpflichtend: den Eintrag des Workflows wählen (heißt in der Liste etwa `test / test (pull_request)`).
- Merge nur durch Detmud.

Erst danach geht kein ungetesteter Stand mehr live, denn jeder Merge nach `main` löst sofort das Deploy in Komodo aus.

## Lokal dasselbe ausführen

```bash
bash tests/ci.sh                  # braucht python3 (3.12), python3-venv, dovecot-imapd, openssl, optional rspamd
CI_SKIP_RSPAMD=1 bash tests/ci.sh # ohne echten Rspamd
```

Das Skript muss als root laufen, weil Dovecot auf den Benutzer `vmail` wechselt (legt ihn bei Bedarf an). Die Ports 10143 und 11333 müssen frei sein. Alles landet in einem Temp-Verzeichnis, das am Ende samt Dovecot und Rspamd wieder verschwindet.
