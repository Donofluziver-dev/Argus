# Anleitung: Argus auf einem Linux-Server installieren

Diese Anleitung führt Schritt für Schritt durch die Installation auf einem Linux-Server mit
NVIDIA-Grafikkarte, zum Beispiel einer RTX 6000 mit 94 GB. Am Ende läuft Argus auf dem Server,
und du öffnest es im Browser auf deinem eigenen PC.

Dauer: rund eine Stunde, das meiste davon Downloads (Modelle rund 27 GB, Images mit den
GPU-Varianten rund 25 GB) und der Bau der Images. Du musst nur am Anfang ein paar Fragen beantworten.

---

## 1. Was der Server braucht

| | Mindestens | Prüfen mit |
|---|---|---|
| Linux, x86_64 | Ubuntu 22.04/24.04, Debian 12 oder ähnlich | `uname -m` → `x86_64` |
| NVIDIA-Grafikkarte, Treiber | Treiber 570 oder neuer (das Modell-Image braucht CUDA 12.8) | `nvidia-smi` |
| Docker mit Compose-Plugin | Docker Engine 24+, `docker compose` (v2) | `docker compose version` |
| NVIDIA Container Toolkit | installiert und für Docker eingerichtet | siehe Schritt 2 |
| Arbeitsspeicher | 32 GB | `free -g` |
| Platz | rund 30 GB für Modelle, rund 40 GB für Images | `df -h ~ /var/lib/docker` |
| Internet | GitHub, Hugging Face, Docker Hub, ghcr.io, PyPI, Debian-Quellen | Schritt 2 prüft das |
| Dein Konto | in der Gruppe `docker`, nicht root | `groups` zeigt `docker` |

Fehlt etwas davon, sagt dir die Vorprüfung in Schritt 2 genau was und wie du es behebst. Du
musst diese Tabelle also nicht vorher abarbeiten.

**Konto in die Gruppe `docker`** (einmalig, danach ab- und wieder anmelden):

```bash
sudo usermod -aG docker $USER
```

---

## 2. Vorprüfung (ändert nichts)

Per SSH auf dem Server anmelden und zuerst nur prüfen lassen:

```bash
curl -fsSL https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh | ARGUS_NUR_PRUEFEN=1 bash
```

So sieht es aus, wenn alles passt:

```
Argus: Vorpruefung

  [OK]      CPU-Architektur x86_64
  [OK]      Python 3.12.3
  [OK]      Arbeitsspeicher 251 GB
  [OK]      Grafikkarte NVIDIA RTX PRO 6000 Blackwell ..., 94 GB, Treiber 580.xx
  [OK]      Docker 28.x
  [OK]      Docker Compose version v2.x
  [OK]      Platz fuer Images: ... GB frei in /var/lib/docker
  [OK]      NVIDIA Container Toolkit: ein Container sieht die Grafikkarte
  [OK]      Platz fuer Modelle: ... GB frei in /home/<du>/argus
  [OK]      erreichbar: https://github.com
  ...
Vorpruefung bestanden (0 Warnung(en)).
```

Steht irgendwo `[FEHLER]`, steht direkt darunter der Befehl, der es behebt. Die häufigsten
Fälle stehen in [Abschnitt 8](#8-wenn-etwas-nicht-klappt). Danach den Befehl einfach noch
einmal ausführen.

---

## 3. Installieren

```bash
curl -fsSL https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh | bash
```

Das Skript prüft noch einmal, holt den Code nach `~/argus/app` und startet die geführte
Installation. Lieber erst ansehen, was du ausführst? Dann so:

```bash
curl -fsSLO https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh
less bootstrap.sh
bash bootstrap.sh
```

Fehlt auf dem Server `python3-venv`, fragt die Installation zuerst, ob sie es per `apt-get`
nachinstallieren soll (dafür einmal das `sudo`-Passwort).

---

## 4. Die Fragen und was du antwortest

Die Installation läuft in sieben Schritten. Gefragt wird nur in Schritt 3, danach läuft sie
allein. Die Vorgabe steht in eckigen Klammern; Enter nimmt sie.

| Frage | Empfehlung für die RTX 6000 | Warum |
|---|---|---|
| Arbeitsordner | Enter (`~/argus/workspace`) | Dokumente und Dateien der Konten |
| Modellordner | Enter (`~/argus/models`) | rund 27 GB |
| Sprachmodell `[1 = Qwen3.6…, 2 = Gemma 4…]` | **1** (Qwen) | liest Scans sicherer; Gemma ist schneller, aber bei schiefen Scans unsicherer |
| Betriebsmodus `[1 = nur dieser Rechner, 2 = Server…]` | **1** | Zugriff per SSH-Tunnel, nichts ist im Netz offen (Abschnitt 5) |
| Spracheingabe und Vorlesen? | **j** | lädt beim Bau 4,4 GB |
| Ablaufverfolgung mit Phoenix? | j | zeigt jede Anfrage als Ablauf, nur lokal |
| Websuche und Seitenabruf? | j | ohne geht nichts ins Internet |
| Dokumentensuche auf `[1 = Grafikkarte…, 2 = CPU]` | **1** (Vorschlag) | eine Suche 0,2 s statt 3,5 s |
| Sprache auf `[1 = Grafikkarte…, 2 = CPU]` | **1** (Vorschlag) | Vorlesen etwa 4-mal, Erkennen etwa 8-mal so schnell |
| Sollen die Modelle jetzt heruntergeladen werden? | j | |
| Soll der Container-Stapel jetzt gebaut und gestartet werden? | j | |

Auf der RTX 6000 schlägt die Installation bei Suche und Sprache selbst die Grafikkarte vor.
Das Sprachmodell legt sie komplett auf die Karte, den Bild-Encoder ebenso. Einstellen musst du
nichts. In Schritt 4 zeigt sie die Aufteilung an; vom Grafikspeicher sind dabei die rund 8 GB
für Suche und Sprache schon abgezogen:

```
  Aufteilung fuer 86 GB Grafikspeicher: 0 Schichten mit Experten im RAM, Bild-Encoder auf der Grafikkarte.
```

**Ohne Fragen**, alles mit diesen Antworten:

```bash
curl -fsSL https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh | bash -s -- --yes --llm qwen --suche gpu --sprache gpu
```

**Was dann passiert:** Modelle laden (Fortschritt wird angezeigt, abgebrochene Downloads
setzen beim nächsten Lauf fort), Images bauen, Stapel starten. Der Bau des Vorlesens für die
Grafikkarte lädt einmal das CUDA-Entwicklungs-Image von NVIDIA (rund 4 GB) und kompiliert
einige Minuten. Zum Schluss wartet die Installation, bis Argus antwortet (beim ersten Start
lädt das Modell eine Weile), und meldet:

```
[7/7] Abschluss...
Argus ist erreichbar unter: http://127.0.0.1:7860
Der Einrichtungs-Schluessel steht in der .env unter DASHBOARD_SETUP_TOKEN.
```

Die Frage, ob der Browser geöffnet werden soll, beantwortest du auf dem Server mit **n**.

---

## 5. Argus vom eigenen PC öffnen

Im Modus „nur dieser Rechner“ hört Argus nur auf dem Server selbst. Vom eigenen PC kommst du
über einen SSH-Tunnel hin. Auf deinem PC (PowerShell oder Terminal):

```bash
ssh -L 7860:127.0.0.1:7860 <nutzer>@<server>
```

Solange diese Verbindung offen ist, öffnest du im Browser auf deinem PC:

```
http://127.0.0.1:7860
```

Sollen später mehrere Personen ohne Tunnel zugreifen, die Installation noch einmal starten
und Betriebsmodus 2 (Server) mit dem TLS-Proxy wählen; siehe
[Betrieb auf einem Server](../README.de.md#betrieb-auf-einem-server).

---

## 6. Erstes Konto und erste Dokumente

Den Einrichtungs-Schlüssel auf dem Server anzeigen:

```bash
grep DASHBOARD_SETUP_TOKEN ~/argus/app/.env
```

Im Browser Benutzername, Passwort und diesen Schlüssel eingeben. Dieses erste Konto wird
Administrator. Weitere Konten legst du unter *Monitor › Konten* an.

Dokumente: *Monitor › Hochladen*, dann „Ingestieren“. Zum Ausprobieren eignen sich die 30
erfundenen Dokumente in `~/argus/app/eval/retrieval/corpus`. Gescannte PDFs und Fotos gehen
denselben Weg; das Modell liest sie selbst.

---

## 7. Prüfen, aktualisieren, anhalten

Alle Befehle im Ordner `~/argus/app`:

```bash
cd ~/argus/app
./install.sh doctor            # prueft alles, aendert nichts
docker compose ps              # laufen alle Dienste? (healthy)
docker compose logs -f llm     # Log des Sprachmodells
nvidia-smi                     # Grafikspeicher und Auslastung
```

| Was | Befehl |
|---|---|
| Aktualisieren (neuer Stand von GitHub) | den Befehl aus Schritt 3 noch einmal; `.env`, Geheimnisse und Daten bleiben |
| Anhalten / wieder starten | `docker compose stop` / `docker compose up -d` |
| Passwort zurücksetzen | `./install.sh reset-password <konto>` |
| Sprachmodell wechseln | `./install.sh --llm gemma` (oder `--llm qwen`) |
| Suche oder Sprache auf CPU/Grafikkarte umstellen | `./install.sh --suche cpu` bzw. `--sprache gpu` |
| Code-Sandbox unter gVisor (einmal `runsc` einrichten, siehe README, „Code-Sandbox unter gVisor“) | `./install.sh --gvisor`, abschalten mit `--no-gvisor` |

`docker compose down` hält den Stapel an und entfernt die Container; Daten, Konten und
Dokumente bleiben. Die Ordner unter `~/argus` und die Docker-Volumes nur löschen, wenn du
wirklich alles verwerfen willst.

---

## 8. Wenn etwas nicht klappt

| Meldung | Lösung |
|---|---|
| `Kein Zugriff auf Docker: … nicht in der Gruppe docker` | `sudo usermod -aG docker $USER`, ab- und wieder anmelden |
| `Docker laeuft nicht` | `sudo systemctl enable --now docker` |
| `NVIDIA Container Toolkit fehlt` | [Anleitung von NVIDIA](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), dann `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `Ein Container sieht die Grafikkarte nicht` | wie eben: `nvidia-ctk runtime configure`, Docker neu starten |
| `Treiber … ist zu alt` | NVIDIA-Treiber 570 oder neuer installieren, neu starten |
| `nicht erreichbar: https://…` | Firewall oder Proxy. Mit Proxy: `export HTTPS_PROXY=http://proxy:port` vor dem Befehl, dazu Docker den Proxy geben (siehe unten) |
| Installation wartet lange auf „Bereitschaft“ | beim ersten Start normal; sonst `docker compose logs llm` |
| `Zeitueberschreitung beim Warten` | `docker compose ps` zeigt, welcher Dienst hängt; dessen Log ansehen |
| Port 7860 belegt | ein anderer Dienst nutzt ihn; `ss -ltnp | grep 7860` |

**Firmen-Proxy:** Docker braucht ihn an zwei Stellen. Für das Laden von Images in
`/etc/systemd/system/docker.service.d/http-proxy.conf`:

```ini
[Service]
Environment="HTTP_PROXY=http://proxy:3128" "HTTPS_PROXY=http://proxy:3128" "NO_PROXY=localhost,127.0.0.1"
```

danach `sudo systemctl daemon-reload && sudo systemctl restart docker`. Für den Bau der Images
in `~/.docker/config.json`:

```json
{ "proxies": { "default": { "httpProxy": "http://proxy:3128", "httpsProxy": "http://proxy:3128", "noProxy": "localhost,127.0.0.1" } } }
```

**Ohne Internet auf dem Server:** Dann auf einem Rechner mit Internet ein Offline-Paket bauen
(`./install.sh bundle --with-models`, rund 45 GB), auf den Server kopieren und dort
`./install.sh --bundle <ordner>` starten. Das Paket passt nur zur CPU-Art, auf der es gebaut
wurde; Einzelheiten in der [README](../README.de.md#geführte-installation).

---

## Was du danach erwarten kannst

Gemessen auf einer RTX 5070 Ti mit 16 GB; auf der RTX 6000 liegt das Modell ganz auf der Karte
und ist schneller.

| | Wert |
|---|---|
| Antwort auf eine Frage an die Dokumente | rund 20 s (mit Suchen und Nachdenken) |
| Dokumentensuche auf der Grafikkarte | 0,17 s je Suche, 96 % richtiges Dokument an erster Stelle |
| Scan oder Foto abschreiben | 3 bis 15 s je Seite, fehlerfrei bei sauberen Scans ab 150 dpi |
| Spracherkennung auf der Grafikkarte | 12 s Sprache in 0,3 s |
| Vorlesen auf der Grafikkarte | 1 s Sprache in 0,35 s |

Mehr dazu in der [README](../README.de.md#gemessen).
