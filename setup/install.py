#!/usr/bin/env python3
"""Gefuehrte Installation fuer Argus (Container-Stapel und Konfiguration)."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# Ausgabe nur ASCII-sicher oder UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# =============================================================================
# Hilfsfunktionen fuer Eingaben und Benutzerinteraktion
# =============================================================================

def prompt_bool(question: str, default: bool = True, non_interactive: bool = False) -> bool:
    """Fragt eine Ja/Nein-Entscheidung ab."""
    if non_interactive:
        return default
    choice_str = "[J/n]" if default else "[j/N]"
    while True:
        try:
            val = input(f"{question} {choice_str}: ").strip().lower()
        except EOFError:
            return default
        if not val:
            return default
        if val in ("j", "ja", "y", "yes"):
            return True
        if val in ("n", "nein", "no"):
            return False
        print("Bitte mit 'j' (Ja) oder 'n' (Nein) antworten.")


def prompt_choice(question: str, choices: list[str], default: str, non_interactive: bool = False) -> str:
    """Fragt eine Auswahl ab."""
    if non_interactive:
        return default
    while True:
        try:
            val = input(f"{question} [{default}]: ").strip()
        except EOFError:
            return default
        if not val:
            return default
        if val in choices:
            return val
        print(f"Bitte eine der Optionen waehlen: {', '.join(choices)}")


def prompt_text(question: str, default: str, non_interactive: bool = False) -> str:
    """Fragt eine Texteingabe mit Vorgabe ab."""
    if non_interactive:
        return default
    try:
        val = input(f"{question} [{default}]: ").strip()
    except EOFError:
        return default
    return val if val else default


# =============================================================================
# URL-Validierung und Server-Konfiguration
# =============================================================================

def validate_server_url(url: str) -> tuple[bool, str]:
    """Prueft die Server-Adresse: muss mit https:// beginnen, darf keinen Pfad haben."""
    if not url:
        return False, "Server-Adresse darf nicht leer sein."
    if not url.startswith("https://"):
        return False, "Server-Adresse muss mit https:// beginnen."
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        return False, f"Ungueltige URL-Syntax: {exc}"
    if parsed.scheme != "https":
        return False, "Schema muss https sein."
    if not parsed.netloc:
        return False, "Server-Adresse muss einen Hostnamen enthalten."
    if not parsed.hostname:
        return False, "Ungueltiger Hostname in der URL."
    if parsed.path not in ("", "/"):
        return False, "Server-Adresse darf keinen Pfad enthalten."
    if parsed.query:
        return False, "Server-Adresse darf keine Abfrageparameter enthalten."
    if parsed.fragment:
        return False, "Server-Adresse darf kein Fragment (#) enthalten."
    return True, ""


def compute_server_env(url: str) -> dict[str, str]:
    """Erzeugt die .env-Werte fuer den Serverbetrieb."""
    valid, err = validate_server_url(url)
    if not valid:
        raise ValueError(f"Ungueltige Server-Adresse: {err}")
    parsed = urllib.parse.urlsplit(url)
    adresse = f"https://{parsed.netloc}"
    hostname = parsed.hostname
    return {
        "DASHBOARD_ORIGINS": f"{adresse},http://127.0.0.1:7860,http://localhost:7860",
        "TRUSTED_HOSTS": f"{hostname},127.0.0.1,localhost,rag-backend",
        "DASHBOARD_TRACES_URL": "",
    }


# =============================================================================
# Hardware-Pruefung: GPU und RAM
# =============================================================================

def check_gpu() -> tuple[bool, str, int]:
    """Prueft per nvidia-smi Name und Gesamtspeicher der GPU in MiB.
    Liefert (vorhanden, Name, Speicher_in_MiB)."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, check=False
        )
        if res.returncode != 0 or not res.stdout.strip():
            return False, "", 0
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if not lines:
            return False, "", 0
        parts = [p.strip() for p in lines[0].split(",")]
        name = parts[0] if parts else "NVIDIA GPU"
        mem_mib = 0
        if len(parts) > 1:
            m = re.search(r"(\d+)", parts[1])
            if m:
                mem_mib = int(m.group(1))
        return True, name, mem_mib
    except Exception:
        return False, "", 0


def check_ram_bytes() -> int:
    """Ermittelt den gesamten physischen Arbeitsspeicher in Bytes."""
    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
        except Exception:
            pass
    elif os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        m = re.search(r"(\d+)", line)
                        if m:
                            return int(m.group(1)) * 1024
        except Exception:
            pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except Exception:
        return 0


# =============================================================================
# Docker-Pruefung und -Initialisierung
# =============================================================================

def run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Fuehrt einen Befehl aus und liefert (returncode, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode, res.stdout, res.stderr
    except FileNotFoundError:
        return 127, "", "Befehl nicht gefunden"
    except Exception as exc:
        return 1, "", str(exc)


def check_docker_environment(non_interactive: bool = False) -> None:
    """Prueft Docker, Docker Compose und NVIDIA Container Toolkit."""
    rc_ver, _, _ = run_command(["docker", "version"])
    rc_comp, _, _ = run_command(["docker", "compose", "version"])

    if os.name == "nt":
        if rc_ver == 127 or rc_comp != 0:
            print("\nHinweis: Docker Desktop erfordert fuer groessere Firmen (ab 250 Mitarbeiter")
            print("oder ueber 10 Mio. USD Jahresumsatz) ein kostenpflichtiges Abonnement.")
            if not prompt_bool("Soll Docker Desktop jetzt ueber winget installiert werden?",
                               default=True, non_interactive=non_interactive):
                print("Installation von Docker Desktop abgebrochen. Bitte Docker manuell installieren.")
                sys.exit(1)

            # WSL vorher pruefen
            rc_wsl, _, _ = run_command(["wsl", "--status"])
            if rc_wsl != 0:
                if prompt_bool("WSL fehlt. Soll WSL jetzt installiert werden?",
                               default=True, non_interactive=non_interactive):
                    print("Installiere WSL...")
                    subprocess.run(["wsl", "--install", "--no-distribution"], check=False)
                    print("\nBitte starten Sie Ihren Rechner neu und starten Sie install.cmd erneut.")
                    sys.exit(0)
                else:
                    print("WSL wird fuer Docker Desktop benoetigt. Abbruch.")
                    sys.exit(1)

            print("Installiere Docker Desktop ueber winget...")
            w_res = subprocess.run([
                "winget", "install", "-e", "--id", "Docker.DockerDesktop",
                "--accept-package-agreements", "--accept-source-agreements"
            ], check=False)
            if w_res.returncode != 0:
                print("Fehler bei der Installation von Docker Desktop.")
                sys.exit(1)
            print("\nDocker Desktop ist installiert. Bitte jetzt:")
            print("  1. Abmelden und wieder anmelden (oder den Rechner neu starten),")
            print("  2. Docker Desktop einmal starten und die Bedingungen bestaetigen,")
            print("  3. install.cmd erneut starten.")
            sys.exit(0)

        # Pruefen, ob Docker-Dienst laeuft
        rc_info, _, _ = run_command(["docker", "info"])
        if rc_info != 0:
            desktop_exe = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
            if desktop_exe.exists():
                print("Docker-Dienst laeuft noch nicht. Starte Docker Desktop...")
                try:
                    subprocess.Popen([str(desktop_exe)])
                except Exception as exc:
                    print(f"Konnte Docker Desktop nicht starten: {exc}")
                    sys.exit(1)

                print("Warte bis zu 3 Minuten auf Docker-Bereitschaft", end="", flush=True)
                ready = False
                for _ in range(36):
                    time.sleep(5)
                    sys.stdout.write(".")
                    sys.stdout.flush()
                    rc_check, _, _ = run_command(["docker", "info"])
                    if rc_check == 0:
                        ready = True
                        break
                print("")
                if not ready:
                    print("Docker Desktop ist nicht innerhalb von 3 Minuten bereit geworden.")
                    print("Bitte starten Sie Docker Desktop manuell und fuehren Sie install.cmd erneut aus.")
                    sys.exit(1)
            else:
                print("Docker ist installiert, der Dienst laeuft aber nicht.")
                print(f"Docker Desktop wurde unter {desktop_exe} nicht gefunden.")
                print("Bitte starten Sie Docker Desktop manuell.")
                sys.exit(1)

    else:
        # Linux
        if rc_ver != 0 or rc_comp != 0:
            print("\nDocker oder Docker Compose v2 wurde nicht gefunden.")
            print("Anleitung zur Installation:")
            print("  https://docs.docker.com/engine/install/")
            sys.exit(1)

        rc_info, info_out, _ = run_command(["docker", "info"])
        if rc_info != 0:
            print("\nDocker-Dienst laeuft nicht (docker info fehlgeschlagen).")
            print("Bitte starten Sie den Docker-Dienst: sudo systemctl start docker")
            sys.exit(1)

        # NVIDIA Container Toolkit pruefen
        has_nvidia_runtime = "nvidia" in info_out.lower()
        has_nvidia_ctk = shutil.which("nvidia-ctk") is not None
        if not (has_nvidia_runtime or has_nvidia_ctk):
            print("\nWarnung: NVIDIA Container Toolkit fehlt.")
            print("Ohne dieses Toolkit koennen Container nicht auf die NVIDIA-Grafikkarte zugreifen.")
            print("Anleitung: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html")
            if not prompt_bool("Moechten Sie trotzdem fortfahren?", default=False, non_interactive=non_interactive):
                print("Installation abgebrochen.")
                sys.exit(1)


# =============================================================================
# Modelle laden und pruefen
# =============================================================================

def load_models_spec(models_json_path: Path) -> list[dict]:
    """Liest models.json und liefert eine flache Liste aller Zieldateien."""
    if not models_json_path.exists():
        raise FileNotFoundError(f"Modell-Konfiguration nicht gefunden: {models_json_path}")
    data = json.loads(models_json_path.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for model in data.get("models", []):
        repo = model["repo"]
        revision = model["revision"]
        for f in model.get("files", []):
            entries.append({
                "repo": repo,
                "revision": revision,
                "path": f["path"],
                "target": f["target"],
                "size": int(f["size"]),
                "sha256": f["sha256"],
                "model_id": model.get("id", ""),
                "model_title": model.get("title", ""),
            })
    return entries


def verify_file_sha256(path: Path, expected_sha256: str, total_size: int = 0, show_progress: bool = True) -> bool:
    """Berechnet die SHA-256 einer Datei mit Fortschrittsanzeige."""
    hasher = hashlib.sha256()
    read_bytes = 0
    last_print = 0.0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            read_bytes += len(chunk)
            now = time.monotonic()
            if show_progress and (now - last_print >= 1.0 or (total_size and read_bytes == total_size)):
                last_print = now
                pct = (read_bytes / total_size) * 100 if total_size > 0 else 0
                mb = read_bytes / (1024 * 1024)
                sys.stdout.write(f"\r  Pruefe SHA-256: {pct:5.1f}% ({mb:.0f} MB)".ljust(65))
                sys.stdout.flush()
    if show_progress:
        sys.stdout.write("\r" + " " * 65 + "\r")
        sys.stdout.flush()
    return hasher.hexdigest().lower() == expected_sha256.lower()


def check_disk_space(target_dir: Path, total_needed_bytes: int, reserve_bytes: int = 1024**3) -> tuple[bool, int, int]:
    """Prueft freien Speicherplatz (Bedarf + 1 GB Reserve).
    Liefert (genuegend_platz, freie_bytes, benoetigte_bytes)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_dir)
    required = total_needed_bytes + reserve_bytes
    return usage.free >= required, usage.free, required


def download_file_with_resume(
    url: str,
    target_path: Path,
    expected_size: int,
    expected_sha256: str,
    max_retries: int = 5,
    timeout: float = 30.0,
    show_progress: bool = True,
) -> None:
    """Laedt eine Datei mit Range-Header, Fortsetzen und SHA-256-Pruefung."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = target_path.with_name(target_path.name + ".part")

    for attempt in range(1, max_retries + 1):
        try:
            existing_size = 0
            hasher = hashlib.sha256()

            if part_path.exists():
                existing_size = part_path.stat().st_size
                if existing_size > expected_size:
                    part_path.unlink()
                    existing_size = 0
                elif existing_size == expected_size:
                    # Vollstaendig geladen, aber nicht mehr umbenannt: pruefen statt
                    # nachzuladen (ein Range ab Dateiende beantwortet der Server mit 416).
                    if verify_file_sha256(part_path, expected_sha256, total_size=expected_size,
                                          show_progress=show_progress):
                        os.replace(part_path, target_path)
                        return
                    part_path.unlink()
                    existing_size = 0
                elif existing_size > 0:
                    with open(part_path, "rb") as pf:
                        while chunk := pf.read(1024 * 1024):
                            hasher.update(chunk)

            headers = {"User-Agent": "argus-setup"}
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_status = getattr(resp, "status", 200)

                if existing_size > 0 and resp_status == 206:
                    file_mode = "ab"
                    current_downloaded = existing_size
                else:
                    file_mode = "wb"
                    current_downloaded = 0
                    hasher = hashlib.sha256()

                start_session_time = time.monotonic()
                bytes_at_session_start = current_downloaded
                last_print_time = 0.0

                with open(part_path, file_mode) as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        f.write(chunk)
                        current_downloaded += len(chunk)

                        now = time.monotonic()
                        if show_progress and (now - last_print_time >= 1.0 or current_downloaded == expected_size):
                            last_print_time = now
                            elapsed = now - start_session_time
                            bytes_diff = current_downloaded - bytes_at_session_start
                            speed = bytes_diff / elapsed if elapsed > 0 else 0
                            pct = (current_downloaded / expected_size) * 100 if expected_size > 0 else 100
                            rem_bytes = max(0, expected_size - current_downloaded)
                            if speed > 0:
                                rem_secs = int(rem_bytes / speed)
                                m, s = divmod(rem_secs, 60)
                                h, m = divmod(m, 60)
                                eta_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                            else:
                                eta_str = "--:--"
                            speed_mb = speed / (1024 * 1024)
                            curr_mb = current_downloaded / (1024 * 1024)
                            tot_mb = expected_size / (1024 * 1024)
                            line = (f"\r  Lade: {pct:5.1f}% | {curr_mb:.1f}/{tot_mb:.1f} MB | "
                                    f"{speed_mb:.1f} MB/s | Restzeit: {eta_str}")
                            sys.stdout.write(line.ljust(75))
                            sys.stdout.flush()

            if show_progress:
                sys.stdout.write("\r" + " " * 75 + "\r")
                sys.stdout.flush()

            final_size = part_path.stat().st_size
            if final_size != expected_size:
                if part_path.exists():
                    part_path.unlink()
                raise ValueError(f"Dateigroesse nach Download weicht ab ({final_size} != {expected_size}).")

            final_hash = hasher.hexdigest().lower()
            if final_hash != expected_sha256.lower():
                if part_path.exists():
                    part_path.unlink()
                raise ValueError(f"Pruefsummenfehler: Erwartet {expected_sha256}, erhalten {final_hash}.")

            os.replace(part_path, target_path)
            return

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < max_retries:
                if show_progress:
                    print(f"\n  Verbindung unterbrochen ({exc}). Wiederhole Versuch {attempt + 1}/{max_retries}...")
                time.sleep(2)
                continue
            else:
                if show_progress:
                    print(f"\n  Download nach {max_retries} Versuchen fehlgeschlagen: {exc}")
                raise
        except ValueError:
            raise


# =============================================================================
# Geheimnisse und .env (setup/init_stack.py)
# =============================================================================

def ensure_setup_venv(repo_root: Path) -> Path:
    """Richtet .venv-setup ein und installiert cryptography==50.0.2."""
    venv_dir = repo_root / ".venv-setup"
    if os.name == "nt":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    if not venv_python.exists():
        print(f"Erstelle Hilfsumgebung in {venv_dir}...")
        res = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=False)
        if res.returncode != 0:
            raise RuntimeError("Erstellung der virtuellen Umgebung (.venv-setup) fehlgeschlagen.")

    # Pruefen ob cryptography vorhanden ist
    chk = subprocess.run([str(venv_python), "-c", "import cryptography"], capture_output=True, check=False)
    if chk.returncode != 0:
        print("Installiere Hilfspaket 'cryptography==50.0.2' in .venv-setup...")
        pip_res = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--only-binary=:all:", "cryptography==50.0.2", "-q"],
            capture_output=True, text=True, check=False
        )
        if pip_res.returncode != 0:
            raise RuntimeError(f"Installation von cryptography fehlgeschlagen:\n{pip_res.stderr}")

    return venv_python


def run_init_stack(repo_root: Path, venv_python: Path, workspace: str, models: str) -> None:
    """Fuehrt setup/init_stack.py mit dem venv-Python aus und reicht Ausgabe durch."""
    init_script = repo_root / "setup" / "init_stack.py"
    cmd = [str(venv_python), str(init_script), "--workspace", workspace, "--models", models,
           "--ohne-naechste-schritte"]
    # Eigene Ausgabe vorher hinausschreiben, sonst steht die des Unterprozesses davor.
    sys.stdout.flush()
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        raise RuntimeError("setup/init_stack.py wurde mit Fehler beendet.")


def update_server_env(repo_root: Path, server_url: str) -> None:
    """Aktualisiert die Server-Werte in der .env."""
    init_script = repo_root / "setup" / "init_stack.py"
    spec = importlib.util.spec_from_file_location("init_stack", init_script)
    if not spec or not spec.loader:
        raise ImportError("setup/init_stack.py konnte nicht geladen werden.")
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)

    env_path = repo_root / ".env"
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    server_values = compute_server_env(server_url)
    init_mod.write_env_file(env_path, template_text, server_values)
    print("Hinweis: Fuer den Serverbetrieb ist ein TLS-Reverse-Proxy vor Port 7860 noetig "
          "(siehe README, Abschnitt 'Betrieb auf einem Server').")


# =============================================================================
# Container-Stapel starten und Status pruefen
# =============================================================================

def get_compose_status(repo_root: Path) -> str:
    """Liest den aktuellen Status der Dienste per 'docker compose ps --format json'."""
    try:
        res = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=repo_root, capture_output=True, text=True, check=False
        )
        if res.returncode != 0 or not res.stdout.strip():
            return "Dienste werden gestartet..."
        text = res.stdout.strip()
        items = []
        if text.startswith("["):
            items = json.loads(text)
        else:
            for line in text.splitlines():
                if line.strip():
                    items.append(json.loads(line.strip()))
        summary = []
        for item in items:
            name = item.get("Service") or item.get("Name", "dienst")
            state = item.get("State", "")
            health = item.get("Health", "")
            status_str = f"{name}: {state}"
            if health:
                status_str += f" ({health})"
            summary.append(status_str)
        return ", ".join(summary) if summary else "Dienste werden gestartet..."
    except Exception:
        return "Dienste werden gestartet..."


def check_healthz(url: str = "http://127.0.0.1:7860/healthz") -> bool:
    """Fragt den Healthz-Endpunkt von Argus ab."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "argus-setup"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_healthz(repo_root: Path, timeout_seconds: int = 1500) -> bool:
    """Wartet bis zu 25 Minuten auf HTTP 200 an /healthz."""
    start_time = time.monotonic()
    dots = 0
    while time.monotonic() - start_time < timeout_seconds:
        if check_healthz():
            return True
        time.sleep(10)
        dots += 1
        sys.stdout.write(".")
        sys.stdout.flush()
        if dots % 6 == 0:
            status_text = get_compose_status(repo_root)
            mins = dots * 10 // 60
            sys.stdout.write(f"\n  [Status nach {mins} Min.] {status_text}\n  ")
            sys.stdout.flush()
    return False


# =============================================================================
# Hauptablauf
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gefuehrte Installation fuer Argus.")
    parser.add_argument("--yes", "-y", action="store_true", help="Alle Fragen mit Vorgabe/Ja beantworten.")
    parser.add_argument("--workspace", type=str, default=None, help="Arbeitsordner (ARGUS_WORKSPACE_DIR).")
    parser.add_argument("--models", type=str, default=None, help="Modellordner (ARGUS_MODELS_DIR).")
    parser.add_argument("--server-url", type=str, default=None, help="Server-Adresse (z. B. https://argus.example.org).")
    parser.add_argument("--no-models", action="store_true", help="Modelle nicht laden.")
    parser.add_argument("--no-start", action="store_true", help="Stapel nicht starten.")
    parser.add_argument("--models-only", action="store_true", help="Nur Modelle pruefen und laden.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent

    print("=================================================================")
    print("                ARGUS - Gefuehrte Installation                   ")
    print("=================================================================\n")

    # Vorgaben fuer Pfade
    if os.name == "nt":
        default_workspace = r"C:\Argus_Workspace"
        default_models = r"C:\Argus_Models"
    else:
        default_workspace = os.path.expanduser("~/argus/workspace")
        default_models = os.path.expanduser("~/argus/models")

    # Modell-Pfad fuer --models-only Aufruf
    if args.models_only:
        models_dir = args.models or prompt_text("Modellordner", default_models, non_interactive=args.yes)
        models_spec_path = repo_root / "setup" / "models.json"
        _execute_models_step(models_spec_path, models_dir, non_interactive=args.yes)
        print("\nModellpruefung abgeschlossen.")
        return 0

    # -------------------------------------------------------------------------
    # Schritt 1: Docker pruefen
    # -------------------------------------------------------------------------
    print("[1/7] Pruefe Docker-Umgebung...")
    check_docker_environment(non_interactive=args.yes)
    print("  Docker und Docker Compose v2 sind verfuegbar.\n")

    # -------------------------------------------------------------------------
    # Schritt 2: Hardware pruefen (Grafikkarte und Arbeitsspeicher)
    # -------------------------------------------------------------------------
    print("[2/7] Pruefe Hardware (GPU & Arbeitsspeicher)...")
    has_gpu, gpu_name, gpu_vram_mib = check_gpu()
    if not has_gpu:
        print("  Warnung: Keine NVIDIA-Grafikkarte erkannt (nvidia-smi nicht verfuegbar).")
        print("  Das Sprachmodell benoetigt eine NVIDIA-Grafikkarte fuer lokale Inferenz.")
        if not prompt_bool("  Moechten Sie trotzdem fortfahren?", default=False, non_interactive=args.yes):
            print("Installation abgebrochen.")
            return 1
    else:
        gpu_vram_gb = gpu_vram_mib / 1024
        print(f"  Grafikkarte: {gpu_name} ({gpu_vram_gb:.1f} GB VRAM)")
        if gpu_vram_mib < 16000:
            print("  Hinweis: Die Modellwerte sind fuer 16 GB VRAM optimiert.")
            print("  Bei weniger VRAM muss LLM_N_CPU_MOE in der .env hoeher gesetzt werden.")

    total_ram_bytes = check_ram_bytes()
    if total_ram_bytes > 0:
        total_ram_gb = total_ram_bytes / (1024**3)
        print(f"  Arbeitsspeicher: {total_ram_gb:.1f} GB RAM")
        if total_ram_bytes < 30 * (1024**3):
            print("  Warnung: Rechner verfuegt ueber weniger als 32 GB RAM.")
            print("  Teile des Modells liegen im RAM, daher wird mehr Arbeitsspeicher empfohlen.")
    print("")

    # -------------------------------------------------------------------------
    # Schritt 3: Einstellungen abfragen
    # -------------------------------------------------------------------------
    print("[3/7] Konfiguration & Pfade...")
    workspace_dir = args.workspace or prompt_text("Arbeitsordner", default_workspace, non_interactive=args.yes)
    models_dir = args.models or prompt_text("Modellordner", default_models, non_interactive=args.yes)

    server_url = ""
    if args.server_url:
        valid, err = validate_server_url(args.server_url)
        if not valid:
            print(f"Fehler in --server-url: {err}")
            return 1
        server_url = args.server_url
    elif not args.yes:
        mode = prompt_choice("Betriebsmodus [1 = nur dieser Rechner, 2 = Server fuer mehrere Personen]",
                             choices=["1", "2"], default="1", non_interactive=args.yes)
        if mode == "2":
            while True:
                srv_in = prompt_text("Server-Adresse (z. B. https://argus.example.org)", "")
                valid, err = validate_server_url(srv_in)
                if valid:
                    server_url = srv_in
                    break
                print(f"Ungueltige Adresse: {err}")
    print("")

    # -------------------------------------------------------------------------
    # Schritt 4: Geheimnisse und .env anlegen
    # -------------------------------------------------------------------------
    print("[4/7] Erzeuge Geheimnisse und .env...")
    venv_python = ensure_setup_venv(repo_root)
    run_init_stack(repo_root, venv_python, workspace=workspace_dir, models=models_dir)

    if server_url:
        update_server_env(repo_root, server_url)
    print("")

    # -------------------------------------------------------------------------
    # Schritt 5: Modelle pruefen und laden
    # -------------------------------------------------------------------------
    print("[5/7] Pruefe Modelldateien...")
    modelle_ok = True
    if args.no_models:
        print("  Modell-Download uebersprungen (--no-models).\n")
    else:
        models_spec_path = repo_root / "setup" / "models.json"
        modelle_ok = _execute_models_step(models_spec_path, models_dir, non_interactive=args.yes)
        print("")

    # -------------------------------------------------------------------------
    # Schritt 6: Container-Stapel bauen und starten
    # -------------------------------------------------------------------------
    print("[6/7] Start des Container-Stapels...")
    if args.no_start:
        print("  Start des Stapels uebersprungen (--no-start).\n")
    else:
        print("Hinweis: Der erste Bau dauert etwas, der Sprachdienst laedt dabei 4,4 GB.")
        if not modelle_ok:
            print("Achtung: Es fehlen Modelldateien. Ohne sie starten Sprachmodell und")
            print("Dokumentensuche nicht. Erst die Modelle laden: install.cmd --models-only")
        start_now = prompt_bool("Soll der Container-Stapel jetzt gebaut und gestartet werden?",
                                default=modelle_ok, non_interactive=args.yes)
        if start_now:
            print("Fuehre 'docker compose up -d --build' aus...")
            c_res = subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=repo_root, check=False)
            if c_res.returncode != 0:
                print("Fehler beim Starten des Container-Stapels mit docker compose.")
                return 1

            print("Warte auf Bereitschaft von Argus (bis zu 25 Minuten)...", end="", flush=True)
            if wait_for_healthz(repo_root, timeout_seconds=1500):
                print("\n  Argus ist erfolgreich gestartet und bereit.")
            else:
                print("\n  Zeitueberschreitung beim Warten auf die Bereitschaft von Argus.")
                print("  Bitte pruefen Sie den Zustand der Container mit:")
                print("    docker compose ps")
                print("    docker compose logs llm")
        print("")

    # -------------------------------------------------------------------------
    # Schritt 7: Abschluss
    # -------------------------------------------------------------------------
    print("[7/7] Abschluss...")
    final_url = server_url.rstrip("/") if server_url else "http://127.0.0.1:7860"
    print(f"Argus ist erreichbar unter: {final_url}")
    print("Der Einrichtungs-Schluessel steht in der .env unter DASHBOARD_SETUP_TOKEN.")
    print("Hinweis: Das erste im Browser angelegte Konto wird automatisch Administrator.")

    # Einrichtungs-Schluessel in Zwischenablage kopieren (nur Windows)
    if os.name == "nt":
        init_script = repo_root / "setup" / "init_stack.py"
        spec = importlib.util.spec_from_file_location("init_stack", init_script)
        if spec and spec.loader:
            init_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(init_mod)
            env_file = repo_root / ".env"
            if env_file.exists():
                env_data = init_mod.parse_env(env_file.read_bytes().decode("utf-8"))
                token = env_data.get("DASHBOARD_SETUP_TOKEN", "")
                if token:
                    copy_clip = prompt_bool("Soll der Einrichtungs-Schluessel in die Zwischenablage kopiert werden?",
                                            default=True, non_interactive=args.yes)
                    if copy_clip:
                        try:
                            subprocess.run(["clip"], input=token.encode("utf-8"), check=True)
                            print("Der Einrichtungs-Schluessel wurde in die Zwischenablage kopiert.")
                        except Exception as exc:
                            print(f"Konnte nicht in Zwischenablage kopieren: {exc}")

    open_now = prompt_bool("Soll die Oberflaeche jetzt im Browser geoeffnet werden?",
                           default=True, non_interactive=args.yes)
    if open_now:
        webbrowser.open(final_url)

    print("\nInstallation erfolgreich abgeschlossen.")
    return 0


def _execute_models_step(models_spec_path: Path, models_dir_str: str, non_interactive: bool = False) -> bool:
    """Fuehrt Pruefung und ggf. Download der Modelle durch. True = alle Dateien liegen
    geprueft im Modellordner."""
    models_dir = Path(models_dir_str)
    entries = load_models_spec(models_spec_path)
    to_download: list[dict] = []
    fehlerhaft = False

    for item in entries:
        target = models_dir / item["target"]
        if target.exists():
            if target.stat().st_size == item["size"]:
                print(f"  Pruefe {item['target']} ({item['size'] // (1024*1024)} MB)...")
                if verify_file_sha256(target, item["sha256"], total_size=item["size"], show_progress=True):
                    print(f"  {item['target']}: vorhanden, geprueft")
                    continue
                else:
                    print(f"  {item['target']}: Pruefsumme ungueltig.")
                    if prompt_bool(f"  Soll {item['target']} neu heruntergeladen werden?",
                                   default=True, non_interactive=non_interactive):
                        to_download.append(item)
                    else:
                        fehlerhaft = True
            else:
                print(f"  {item['target']}: Dateigroesse weicht ab ({target.stat().st_size} != {item['size']}).")
                if prompt_bool(f"  Soll {item['target']} neu heruntergeladen werden?",
                               default=True, non_interactive=non_interactive):
                    to_download.append(item)
                else:
                    fehlerhaft = True
        else:
            to_download.append(item)

    if not to_download:
        if fehlerhaft:
            print("  Mindestens eine Modelldatei ist fehlerhaft und wurde nicht neu geladen.")
            return False
        print("  Alle Modelldateien sind vorhanden und geprueft.")
        return True

    # Freien Speicherplatz pruefen
    total_needed = sum(item["size"] for item in to_download)
    has_space, free_bytes, required_bytes = check_disk_space(models_dir, total_needed, reserve_bytes=1024**3)
    if not has_space:
        free_gb = free_bytes / (1024**3)
        req_gb = required_bytes / (1024**3)
        print(f"\nFehler: Zu wenig freier Speicherplatz auf dem Ziellaufwerk!")
        print(f"Benoetigt: {req_gb:.2f} GB (inkl. 1 GB Reserve), verfuegbar: {free_gb:.2f} GB.")
        return False

    total_gb = total_needed / (1024**3)
    print(f"\n  Es muessen {len(to_download)} Modelldatei(en) ({total_gb:.2f} GB) geladen werden.")
    download_now = prompt_bool("  Sollen die Modelle jetzt heruntergeladen werden?",
                               default=True, non_interactive=non_interactive)
    if not download_now:
        print("\nHinweis: Bitte laden Sie folgende Dateien manuell herunter:")
        for item in to_download:
            url = f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{item['path']}"
            dst = models_dir / item["target"]
            print(f"  URL:  {url}")
            print(f"  Ziel: {dst}\n")
        return False

    alle_da = True
    for idx, item in enumerate(to_download, start=1):
        target = models_dir / item["target"]
        url = f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{item['path']}"
        print(f"\n[{idx}/{len(to_download)}] Lade {item['target']}...")
        try:
            download_file_with_resume(
                url=url,
                target_path=target,
                expected_size=item["size"],
                expected_sha256=item["sha256"],
                max_retries=5,
                timeout=30.0,
                show_progress=True,
            )
            print(f"  {item['target']}: erfolgreich heruntergeladen und geprueft.")
        except Exception as exc:
            print(f"  Fehler beim Laden von {item['target']}: {exc}")
            print("  Abbruch dieser Datei.")
            alle_da = False
    return alle_da


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInstallation abgebrochen. Fuehren Sie 'install.cmd' (Windows) bzw. 'install.sh' (Linux) erneut aus.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nUnerwarteter Fehler: {exc}")
        sys.exit(1)
