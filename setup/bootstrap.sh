#!/usr/bin/env bash
# Argus auf einem Linux-Server mit einem Befehl einrichten: Vorpruefung, Code holen,
# gefuehrte Installation starten (die laedt Modelle und baut die Images).
#
#   curl -fsSL https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh | bash
#
# Ohne "curl | bash":
#   curl -fsSLO https://raw.githubusercontent.com/Donofluziver-dev/Argus/main/setup/bootstrap.sh
#   bash bootstrap.sh
#
# Optionen gehen an install.sh, etwa:  bash bootstrap.sh --llm gemma
# Mit "curl | bash" so:                 curl -fsSL <adresse> | bash -s -- --llm gemma
#
# Umgebung:
#   ARGUS_DIR          Ordner fuer den Code (Vorgabe ~/argus/app; Modelle und Arbeitsordner
#                      legt die Installation unter ~/argus an)
#   ARGUS_REF          Branch oder Tag (Vorgabe main)
#   ARGUS_REPO         Repo (Vorgabe https://github.com/Donofluziver-dev/Argus)
#   ARGUS_NUR_PRUEFEN  1 = nur die Vorpruefung, nichts holen oder installieren
#   ARGUS_TROTZDEM     1 = auch bei Fehlern in der Vorpruefung weitermachen
set -euo pipefail

REPO="${ARGUS_REPO:-https://github.com/Donofluziver-dev/Argus}"
REF="${ARGUS_REF:-main}"
ZIEL="${ARGUS_DIR:-$HOME/argus/app}"
DATEN="$(dirname "$ZIEL")"

fehler=0
warnungen=0
ok()      { printf '  [OK]      %s\n' "$*"; }
warn()    { printf '  [WARNUNG] %s\n' "$*"; warnungen=$((warnungen + 1)); }
fehl()    { printf '  [FEHLER]  %s\n' "$*"; fehler=$((fehler + 1)); }
hinweis() { printf '            %s\n' "$*"; }

frei_gb() {
    # Freier Platz in GB fuer einen Pfad; bei einem fehlenden Pfad der naechste Elternordner.
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    df -Pk "$p" 2>/dev/null | awk 'NR == 2 { printf "%d", $4 / 1048576 }'
}

echo "Argus: Vorpruefung"
echo

# ---- System -----------------------------------------------------------------------
if [ "$(uname -s)" != "Linux" ]; then
    fehl "Kein Linux ($(uname -s)). Unter Windows install.cmd nutzen."
fi
arch="$(uname -m)"
case "$arch" in
    x86_64|amd64) ok "CPU-Architektur $arch" ;;
    aarch64|arm64) warn "CPU-Architektur $arch: Alle Images gibt es auch fuer ARM, getestet ist Argus aber auf x86_64." ;;
    *) fehl "CPU-Architektur $arch wird nicht unterstuetzt." ;;
esac
if [ "$(id -u)" -eq 0 ]; then
    warn "Laeuft als root. Besser als normaler Nutzer in der Gruppe docker; die Daten gehoeren sonst root."
fi

for werkzeug in curl tar awk df; do
    command -v "$werkzeug" >/dev/null 2>&1 || fehl "$werkzeug fehlt (ueber den Paketmanager installieren)."
done
if python3 -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    ok "Python $(python3 -c 'import platform; print(platform.python_version())')"
else
    warn "Python ab 3.10 mit venv fehlt. Die Installation bietet an, python3-venv per apt-get nachzuinstallieren (braucht sudo)."
fi

ram_gb="$(awk '/^MemTotal:/ { printf "%d", $2 / 1048576 }' /proc/meminfo 2>/dev/null || echo 0)"
if [ "${ram_gb:-0}" -ge 30 ]; then
    ok "Arbeitsspeicher ${ram_gb} GB"
else
    warn "Arbeitsspeicher ${ram_gb:-?} GB, empfohlen sind 32 GB."
fi

# ---- Grafikkarte -------------------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    while IFS=',' read -r name mem treiber; do
        name="$(echo "$name" | xargs)"; mem="$(echo "$mem" | xargs)"; treiber="$(echo "$treiber" | xargs)"
        if [[ "$mem" =~ ^[0-9]+$ ]]; then
            ok "Grafikkarte $name, $((mem / 1024)) GB, Treiber $treiber"
        else
            ok "Grafikkarte $name (Speicher nicht gemeldet), Treiber $treiber"
        fi
        if [ "${treiber%%.*}" -lt 570 ] 2>/dev/null; then
            fehl "Treiber $treiber ist zu alt: Das Modell-Image braucht CUDA 12.8, also Treiber ab 570."
        fi
    done < <(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -n 1)
else
    fehl "Keine NVIDIA-Grafikkarte oder kein Treiber (nvidia-smi fehlt oder meldet einen Fehler)."
fi

# ---- Docker ------------------------------------------------------------------------
docker_ok=0
if ! command -v docker >/dev/null 2>&1; then
    fehl "Docker fehlt."
    hinweis "Docker Engine mit Compose-Plugin: https://docs.docker.com/engine/install/"
elif ! docker info >/dev/null 2>&1; then
    if [ -S /var/run/docker.sock ] && [ ! -w /var/run/docker.sock ]; then
        fehl "Kein Zugriff auf Docker: $(id -un) ist nicht in der Gruppe docker."
        hinweis "sudo usermod -aG docker $(id -un)   danach ab- und wieder anmelden"
    else
        fehl "Docker laeuft nicht."
        hinweis "sudo systemctl enable --now docker"
    fi
else
    docker_ok=1
    ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    if docker compose version >/dev/null 2>&1; then
        ok "$(docker compose version 2>/dev/null | head -n 1)"
    else
        fehl "Docker Compose v2 fehlt."
        hinweis "Paket docker-compose-plugin installieren."
    fi
    root_dir="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    frei="$(frei_gb "$root_dir")"
    if [ "${frei:-0}" -ge 40 ]; then
        ok "Platz fuer Images: ${frei} GB frei in $root_dir"
    else
        warn "Platz fuer Images: nur ${frei:-?} GB frei in $root_dir, die Images brauchen rund 35 GB."
    fi
fi

if command -v nvidia-ctk >/dev/null 2>&1 || command -v nvidia-container-cli >/dev/null 2>&1 \
        || command -v nvidia-container-runtime-hook >/dev/null 2>&1; then
    if [ "$docker_ok" -eq 1 ]; then
        #--- Der einzig sichere Test: ein Container sieht die Karte (laedt ubuntu, ~30 MB).
        if ! docker image inspect ubuntu:24.04 >/dev/null 2>&1 && ! docker pull -q ubuntu:24.04 >/dev/null 2>&1; then
            warn "NVIDIA Container Toolkit installiert; Test nicht moeglich, ubuntu:24.04 laesst sich nicht laden (Docker Hub?)."
        elif docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L >/dev/null 2>&1; then
            ok "NVIDIA Container Toolkit: ein Container sieht die Grafikkarte"
        else
            fehl "Ein Container sieht die Grafikkarte nicht (docker run --gpus all ... nvidia-smi)."
            hinweis "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
        fi
    else
        ok "NVIDIA Container Toolkit installiert"
    fi
else
    fehl "NVIDIA Container Toolkit fehlt."
    hinweis "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    hinweis "danach: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
fi

# ---- Platz fuer Modelle ------------------------------------------------------------
frei="$(frei_gb "$DATEN")"
if [ "${frei:-0}" -ge 30 ]; then
    ok "Platz fuer Modelle: ${frei} GB frei in $DATEN"
else
    fehl "Platz fuer Modelle: nur ${frei:-?} GB frei in $DATEN, gebraucht werden rund 27 GB."
fi

# ---- Netz --------------------------------------------------------------------------
proxy="${HTTPS_PROXY:-${https_proxy:-}}"
if [ -n "$proxy" ]; then
    ok "Proxy aus der Umgebung: ${proxy##*@}"
    hinweis "Docker braucht ihn zusaetzlich: fuer Pulls in /etc/systemd/system/docker.service.d/http-proxy.conf,"
    hinweis "fuer den Bau in ~/.docker/config.json (\"proxies\")."
fi
for ziel in https://github.com https://huggingface.co https://registry-1.docker.io/v2/ https://ghcr.io/v2/ \
            https://pypi.org/simple/ https://files.pythonhosted.org https://deb.debian.org/debian/; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$ziel" 2>/dev/null || true)"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
        ok "erreichbar: $ziel"
    else
        fehl "nicht erreichbar: $ziel"
    fi
done

echo
if [ "$fehler" -gt 0 ]; then
    echo "Vorpruefung: $fehler Fehler, $warnungen Warnung(en)."
    if [ "${ARGUS_TROTZDEM:-0}" != "1" ]; then
        echo "Bitte zuerst beheben, dann diesen Befehl erneut starten (ARGUS_TROTZDEM=1 macht trotzdem weiter)."
        echo "Ohne Internet: Offline-Paket, siehe README.de.md unter \"Ohne Internet installieren\"."
        exit 1
    fi
else
    echo "Vorpruefung bestanden ($warnungen Warnung(en))."
fi
if [ "${ARGUS_NUR_PRUEFEN:-0}" = "1" ]; then
    exit 0
fi

# ---- Code holen --------------------------------------------------------------------
echo
mkdir -p "$DATEN"
if [ -d "$ZIEL/.git" ] && command -v git >/dev/null 2>&1; then
    echo "Aktualisiere den Code in $ZIEL ..."
    git -C "$ZIEL" pull --ff-only
elif command -v git >/dev/null 2>&1 && [ ! -e "$ZIEL" ]; then
    echo "Hole den Code nach $ZIEL ..."
    git clone --depth 1 --branch "$REF" "$REPO" "$ZIEL"
else
    #--- Ohne git: das Archiv von GitHub. Vorhandene .env, API_Tokens/ und Daten bleiben,
    #--- weil das Archiv sie nicht enthaelt.
    echo "Hole den Code als Archiv nach $ZIEL ..."
    mkdir -p "$ZIEL"
    curl -fsSL "$REPO/archive/$REF.tar.gz" | tar -xz --strip-components=1 -C "$ZIEL"
fi

# ---- Installation starten ----------------------------------------------------------
cd "$ZIEL"
echo
echo "Starte die Installation ($ZIEL/install.sh) ..."
if [ -t 0 ]; then
    exec ./install.sh "$@"
elif (exec </dev/tty) 2>/dev/null; then
    #--- Bei "curl | bash" ist die Eingabe das Skript; die Fragen kommen vom Terminal.
    exec ./install.sh "$@" </dev/tty
else
    echo "Kein Terminal fuer Rueckfragen: Installation mit den Vorgaben (--yes)."
    exec ./install.sh --yes "$@"
fi
