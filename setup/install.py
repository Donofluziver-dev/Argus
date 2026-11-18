#!/usr/bin/env python3
"""Gefuehrte Installation und Diagnose fuer Argus (Container-Stapel und Konfiguration)."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import NamedTuple

# Ausgabe nur ASCII-sicher oder UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class DoctorResult(NamedTuple):
    """Ergebnis einer einzelnen Doctor-Pruefung (OK, WARNUNG oder FEHLER)."""
    status: str
    name: str
    message: str

    def format_line(self) -> str:
        return f"{self.status}: [{self.name}] {self.message}"


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


VALID_TLS_MODES = ("internal", "acme", "files")
DEFAULT_EDGE_SUBNET = "172.31.250.0/29"


def _merge_compose_profiles(existing: str, add=(), remove=()) -> str:
    """Setzt Compose-Profile: 'add' kommt hinzu, 'remove' faellt weg, alle anderen
    (etwa 'proxy') bleiben in ihrer Reihenfolge. 'add' darf auch {profil: an/aus} sein.
    Profile duerfen als Liste oder als Komma-Text kommen."""
    def _liste(v) -> list[str]:
        if isinstance(v, str):
            v = v.split(",")
        return [p.strip() for p in v if p and p.strip()]

    if isinstance(add, dict):
        remove = _liste(remove) + [p for p, an in add.items() if not an]
        add = [p for p, an in add.items() if an]
    weg = set(_liste(remove))
    out: list[str] = []
    for p in _liste(existing) + _liste(add):
        if p not in weg and p not in out:
            out.append(p)
    return ",".join(out)


def compute_server_env(
    url: str,
    *,
    use_proxy: bool = False,
    tls_mode: str = "internal",
    existing_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Erzeugt die .env-Werte fuer den Serverbetrieb (optional mit TLS-Proxy)."""
    valid, err = validate_server_url(url)
    if not valid:
        raise ValueError(f"Ungueltige Server-Adresse: {err}")
    parsed = urllib.parse.urlsplit(url)
    adresse = f"https://{parsed.netloc}"
    hostname = parsed.hostname or ""
    out = {
        "DASHBOARD_ORIGINS": f"{adresse},http://127.0.0.1:7860,http://localhost:7860",
        "TRUSTED_HOSTS": f"{hostname},127.0.0.1,localhost,rag-backend",
        "DASHBOARD_TRACES_URL": "",
    }
    if use_proxy:
        mode = (tls_mode or "internal").strip()
        if mode not in VALID_TLS_MODES:
            raise ValueError(f"Ungueltiger TLS-Modus '{mode}' (erlaubt: {', '.join(VALID_TLS_MODES)}).")
        cur = existing_env or {}
        edge_subnet = (cur.get("ARGUS_EDGE_SUBNET") or "").strip() or DEFAULT_EDGE_SUBNET
        out["COMPOSE_PROFILES"] = _merge_compose_profiles(cur.get("COMPOSE_PROFILES", ""), "proxy")
        out["ARGUS_DOMAIN"] = hostname
        out["ARGUS_TLS_MODE"] = mode
        out["ARGUS_TRUSTED_PROXIES"] = edge_subnet
        #--- Eine Adresse mit Port (https://ki.firma.de:8443) heisst: Caddy auf diesem Port.
        if parsed.port:
            out["ARGUS_PROXY_HTTPS_PORT"] = str(parsed.port)
    return out


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
            stat_mem = MEMORYSTATUSEX()
            stat_mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat_mem)):
                return int(stat_mem.ullTotalPhys)
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


def docker_permission_hint(stderr: str) -> str | None:
    """Unter Linux: 'permission denied' am Docker-Socket heisst, der Benutzer ist nicht
    in der Gruppe docker -- nicht, dass der Dienst steht."""
    low = (stderr or "").lower()
    if "permission denied" in low and "docker" in low:
        return ("Ihr Benutzer darf Docker nicht ansprechen. Naechster Schritt: "
                "sudo usermod -aG docker $USER, danach ab- und wieder anmelden "
                "(oder newgrp docker) und ./install.sh ohne sudo erneut starten.")
    return None


def warn_if_sudo(non_interactive: bool = False, env: dict[str, str] | None = None,
                 euid: int | None = None) -> bool:
    """Warnt, wenn die Installation unter Linux ueber sudo laeuft. Dann gehoeren .env
    und API_Tokens/ root, und jeder spaetere docker-compose-Aufruf braucht ebenfalls
    sudo. Gibt False zurueck, wenn abgebrochen werden soll."""
    env = os.environ if env is None else env
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 1000
    if os.name == "nt" or euid != 0 or not env.get("SUDO_USER"):
        return True
    print("Hinweis: Die Installation laeuft ueber sudo. Dann gehoeren .env und API_Tokens/")
    print("root, und auch spaetere Aufrufe von docker compose brauchen sudo. Empfohlen ist,")
    print("den Benutzer in die Gruppe docker aufzunehmen und ./install.sh ohne sudo zu starten:")
    print("  sudo usermod -aG docker $USER   (danach ab- und wieder anmelden)")
    if non_interactive:
        return True
    return prompt_bool("Trotzdem mit sudo fortfahren?", default=False)


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

        rc_info, info_out, info_err = run_command(["docker", "info"])
        if rc_info != 0:
            hint = docker_permission_hint(info_err)
            if hint:
                print(f"\n{hint}")
            else:
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
# Geheimnisse, .env und Laufzeit-Ordner (setup/init_stack.py)
# =============================================================================

def _load_init_stack_module(repo_root: Path):
    """Laedt setup/init_stack.py dynamisch."""
    init_script = repo_root / "setup" / "init_stack.py"
    spec = importlib.util.spec_from_file_location("init_stack", init_script)
    if not spec or not spec.loader:
        raise ImportError("setup/init_stack.py konnte nicht geladen werden.")
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)
    return init_mod


_COMPOSE_DIR_DEFAULT_RE = re.compile(r"\$\{(ARGUS_[A-Z_]+_DIR):-([^}]+)\}")


def _compose_dir_defaults(repo_root: Path) -> dict[str, str]:
    """Standardwerte der Ordner aus docker-compose.yml (${ARGUS_..._DIR:-<Pfad>}).
    Fehlt ein Schluessel in der .env, nimmt Compose diesen Wert. Aus der Datei
    gelesen statt hier wiederholt, damit beide nicht auseinanderlaufen."""
    compose_path = repo_root / "docker-compose.yml"
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {k: v.strip() for k, v in _COMPOSE_DIR_DEFAULT_RE.findall(text)}


def _effective_dir(repo_root: Path, env_data: dict[str, str], key: str,
                   windows: bool | None = None) -> tuple[str, bool]:
    """Ordner, den Compose fuer <key> tatsaechlich einbindet: (Pfad, aus Standardwert?).

    Die Standardwerte sind Windows-Pfade. Unter Linux taugen sie nicht: Dort gilt ein
    fehlender Schluessel als nicht gesetzt (leerer Pfad)."""
    val = (env_data.get(key) or "").strip()
    if val:
        return val, False
    if windows is None:
        windows = os.name == "nt"
    if windows:
        default = _compose_dir_defaults(repo_root).get(key, "")
        if default:
            return default, True
    return "", False


def _load_check_networks_module(repo_root: Path):
    """Laedt setup/check_networks.py dynamisch."""
    net_script = repo_root / "setup" / "check_networks.py"
    spec = importlib.util.spec_from_file_location("check_networks", net_script)
    if not spec or not spec.loader:
        raise ImportError("setup/check_networks.py konnte nicht geladen werden.")
    net_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(net_mod)
    return net_mod


SETUP_PIP_REQUIREMENT = "cryptography==50.0.2"


def ensure_setup_venv(repo_root: Path, wheels_dir: Path | None = None) -> Path:
    """Richtet .venv-setup ein und installiert cryptography==50.0.2 -- aus dem Netz oder,
    beim Offline-Paket, aus dessen Ordner wheels/."""
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
        print(f"Installiere Hilfspaket '{SETUP_PIP_REQUIREMENT}' in .venv-setup...")
        offline = ["--no-index", "--find-links", str(wheels_dir)] if wheels_dir and wheels_dir.is_dir() else []
        pip_res = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--only-binary=:all:", *offline,
             SETUP_PIP_REQUIREMENT, "-q"],
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


def ensure_installation_dirs(
    repo_root: Path,
    workspace_dir: str,
    models_dir: str,
    evals_dir: str | None = None,
) -> list[Path]:
    """Legt vor dem Start fehlende Ordner an: Arbeitsordner, darin Uploads,
    den Evals-Ordner und den Modellordner."""
    init_mod = _load_init_stack_module(repo_root)
    if evals_dir is None:
        env_path = repo_root / ".env"
        if env_path.exists():
            env_data = init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
            evals_dir = env_data.get("ARGUS_EVALS_DIR")
        if not evals_dir:
            evals_dir = str(Path(workspace_dir) / "Evals")
    return init_mod.ensure_runtime_dirs(
        workspace=Path(workspace_dir),
        evals=Path(evals_dir),
        models=Path(models_dir),
    )


def update_server_env(
    repo_root: Path,
    server_url: str,
    use_proxy: bool = False,
    tls_mode: str = "internal",
) -> dict[str, str]:
    """Aktualisiert die Server-Werte (und optional den TLS-Proxy) in der .env."""
    init_mod = _load_init_stack_module(repo_root)
    env_path = repo_root / ".env"
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    existing_env = (
        init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
        if env_path.exists()
        else init_mod.parse_env(template_text)
    )
    server_values = compute_server_env(
        server_url,
        use_proxy=use_proxy,
        tls_mode=tls_mode,
        existing_env=existing_env,
    )
    init_mod.write_env_file(env_path, template_text, server_values)
    if not use_proxy:
        print("Hinweis: Fuer den Serverbetrieb ist ein TLS-Reverse-Proxy vor Port 7860 noetig "
              "(siehe README, Abschnitt 'Betrieb auf einem Server').")
        return server_values

    #--- Unter Linux muss der Datenordner dem Nutzer gehoeren, unter dem Caddy laeuft;
    #--- legte ihn erst Docker an, gehoerte er root.
    data_dir, tls_dir = proxy_dirs(repo_root, {**existing_env, **server_values})
    init_mod.ensure_runtime_dirs(extra=[data_dir, tls_dir])
    port = server_values.get("ARGUS_PROXY_HTTPS_PORT") or existing_env.get("ARGUS_PROXY_HTTPS_PORT") or "443"
    print(f"TLS-Proxy: Caddy nimmt HTTPS fuer {server_values['ARGUS_DOMAIN']} auf Port {port} an "
          f"(Modus {tls_mode}).")
    if tls_mode == "files":
        print(f"  Zertifikat (mit Zwischenzertifikaten) und Schluessel ablegen als "
              f"{tls_dir / 'cert.pem'} und {tls_dir / 'key.pem'}.")
    elif tls_mode == "internal":
        print("  Caddy stellt das Zertifikat mit einer eigenen CA aus. Deren Stammzertifikat liegt nach dem")
        print(f"  ersten Start unter {data_dir / 'caddy' / 'pki' / 'authorities' / 'local' / 'root.crt'}")
        print("  und muss auf den Rechnern der Nutzer als vertrauenswuerdig eingetragen werden.")
    else:
        print("  Let's Encrypt braucht einen oeffentlichen DNS-Namen und die Ports 80 und 443 aus dem Internet.")
    return server_values


def update_compose_profiles(
    repo_root: Path,
    *,
    use_voice: bool = True,
    use_tracing: bool = True,
    use_web: bool = True,
) -> str:
    """Setzt die Profile 'voice', 'tracing' und 'web' in COMPOSE_PROFILES der .env und
    behaelt andere Profile (etwa 'proxy') bei."""
    init_mod = _load_init_stack_module(repo_root)
    env_path = repo_root / ".env"
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    existing_env = (
        init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
        if env_path.exists()
        else init_mod.parse_env(template_text)
    )
    merged = _merge_compose_profiles(
        existing_env.get("COMPOSE_PROFILES", ""),
        {"voice": use_voice, "tracing": use_tracing, "web": use_web},
    )
    init_mod.write_env_file(env_path, template_text, {"COMPOSE_PROFILES": merged})
    return merged


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
# Diagnose-Pruefungen (doctor: nur lesen, nichts aendern)
# =============================================================================

def doctor_check_docker() -> DoctorResult:
    """Prueft Docker, Docker Compose v2, Erreichbarkeit des Dienstes und unter
    Linux das NVIDIA Container Toolkit."""
    rc_ver, _, _ = run_command(["docker", "version"])
    rc_comp, _, _ = run_command(["docker", "compose", "version"])
    if rc_ver != 0 or rc_comp != 0:
        return DoctorResult(
            "FEHLER",
            "Docker",
            "Docker oder Docker Compose v2 fehlt. Naechster Schritt: Docker installieren (https://docs.docker.com/engine/install/).",
        )

    rc_info, info_out, info_err = run_command(["docker", "info"])
    if rc_info != 0:
        perm = docker_permission_hint(info_err) if os.name != "nt" else None
        if perm:
            return DoctorResult("FEHLER", "Docker", perm)
        hint = "Docker Desktop starten" if os.name == "nt" else "sudo systemctl start docker"
        return DoctorResult(
            "FEHLER",
            "Docker",
            f"Docker-Dienst ist nicht erreichbar. Naechster Schritt: {hint}.",
        )

    if os.name != "nt":
        has_nvidia_runtime = "nvidia" in info_out.lower()
        has_nvidia_ctk = shutil.which("nvidia-ctk") is not None
        if not (has_nvidia_runtime or has_nvidia_ctk):
            return DoctorResult(
                "FEHLER",
                "Docker",
                "NVIDIA Container Toolkit fehlt unter Linux. Naechster Schritt: nvidia-container-toolkit installieren und Docker neu starten.",
            )

    return DoctorResult("OK", "Docker", "Docker und Docker Compose v2 sind erreichbar.")


def doctor_check_gpu() -> DoctorResult:
    """Prueft per nvidia-smi, ob eine NVIDIA-Grafikkarte vorhanden ist."""
    has_gpu, gpu_name, vram_mib = check_gpu()
    if not has_gpu:
        return DoctorResult(
            "FEHLER",
            "Grafikkarte",
            "Keine NVIDIA-Grafikkarte erkannt (nvidia-smi fehlgeschlagen). Naechster Schritt: NVIDIA-Treiber pruefen.",
        )
    vram_gb = vram_mib / 1024
    if vram_mib < 16000:
        return DoctorResult(
            "WARNUNG",
            "Grafikkarte",
            f"{gpu_name} ({vram_gb:.1f} GB VRAM) erkannt; unter 16 GB VRAM ggf. LLM_N_CPU_MOE in der .env erhoehen.",
        )
    return DoctorResult("OK", "Grafikkarte", f"{gpu_name} ({vram_gb:.1f} GB VRAM).")


def doctor_check_ram() -> DoctorResult:
    """Prueft den physischen Arbeitsspeicher."""
    ram_bytes = check_ram_bytes()
    if ram_bytes <= 0:
        return DoctorResult("WARNUNG", "Arbeitsspeicher", "Arbeitsspeicher konnte nicht ermittelt werden.")
    ram_gb = ram_bytes / (1024**3)
    if ram_bytes < 30 * (1024**3):
        return DoctorResult(
            "WARNUNG",
            "Arbeitsspeicher",
            f"{ram_gb:.1f} GB RAM erkannt (mindestens 32 GB empfohlen).",
        )
    return DoctorResult("OK", "Arbeitsspeicher", f"{ram_gb:.1f} GB RAM.")


def doctor_check_disk_space(models_dir: Path, models_spec_path: Path | None = None) -> DoctorResult:
    """Prueft freien Platz im Modellordner, ohne Ordner anzulegen."""
    probe = models_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.exists():
        return DoctorResult(
            "FEHLER",
            "Speicherplatz",
            f"Pfad fuer Modellordner '{models_dir}' existiert nicht.",
        )

    needed_bytes = 0
    if models_spec_path and models_spec_path.exists():
        try:
            for item in load_models_spec(models_spec_path):
                target = models_dir / item["target"]
                if not target.exists() or target.stat().st_size != item["size"]:
                    needed_bytes += item["size"]
        except Exception:
            pass

    usage = shutil.disk_usage(probe)
    reserve_bytes = 1024**3
    required_bytes = needed_bytes + reserve_bytes
    free_gb = usage.free / (1024**3)
    req_gb = required_bytes / (1024**3)
    if usage.free < required_bytes:
        return DoctorResult(
            "FEHLER",
            "Speicherplatz",
            f"Zu wenig freier Platz in '{models_dir}': {free_gb:.1f} GB frei, {req_gb:.1f} GB benoetigt.",
        )
    return DoctorResult("OK", "Speicherplatz", f"{free_gb:.1f} GB frei fuer Modellordner '{models_dir}'.")


def doctor_check_env(repo_root: Path) -> DoctorResult:
    """Prueft .env, fehlende Schluessel aus .env.example und ARGUS_*_DIR-Pfade.
    Gibt niemals Werte von Geheimnissen aus."""
    env_path = repo_root / ".env"
    template_path = repo_root / ".env.example"
    if not env_path.exists():
        return DoctorResult(
            "FEHLER",
            ".env",
            ".env fehlt. Naechster Schritt: python setup/init_stack.py ausfuehren.",
        )

    init_mod = _load_init_stack_module(repo_root)
    env_data = init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
    template_data = (
        init_mod.parse_env(template_path.read_bytes().decode("utf-8"))
        if template_path.exists()
        else {}
    )

    dir_keys = sorted(
        {
            k for k in (set(template_data) | set(env_data))
            if k.startswith("ARGUS_") and k.endswith("_DIR") and k != "ARGUS_TLS_DIR"
        }
        | {"ARGUS_MODELS_DIR", "ARGUS_WORKSPACE_DIR", "ARGUS_EVALS_DIR"}
    )
    dir_errors: list[str] = []
    #--- Unter Windows nimmt Compose fuer einen fehlenden Ordner-Schluessel seinen
    #--- Standardwert; das ist kein Fehler, solange der Ordner da ist.
    dir_notes: list[str] = []
    for key in dir_keys:
        val, from_default = _effective_dir(repo_root, env_data, key)
        if not val:
            dir_errors.append(f"{key} nicht gesetzt")
            continue
        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", val):
            dir_errors.append(f"{key} steht unter Linux auf einem Windows-Pfad")
            continue
        if not Path(val).is_dir():
            dir_errors.append(f"Ordner fuer {key} fehlt ({val})" if from_default else f"Ordner fuer {key} fehlt")
            continue
        if from_default:
            dir_notes.append(f"{key} nicht gesetzt, Compose nimmt {val}")

    noted = {n.split(" ", 1)[0] for n in dir_notes}
    missing_keys = [k for k in template_data if k not in env_data and k not in noted]

    if dir_errors:
        msg = "; ".join(dir_errors)
        if missing_keys:
            msg += f" (zudem fehlen aus .env.example: {', '.join(missing_keys)})"
        msg += ". Naechster Schritt: Pfade in .env pruefen oder python setup/init_stack.py ausfuehren."
        return DoctorResult("FEHLER", ".env", msg)

    raw_ver = env_data.get("ARGUS_ENV_VERSION")
    try:
        env_ver = int(raw_ver) if raw_ver is not None else 0
    except ValueError:
        env_ver = 0

    has_version_req = "ARGUS_ENV_VERSION" in template_data or raw_ver is not None
    missing_keys_no_ver = [k for k in missing_keys if k != "ARGUS_ENV_VERSION"]

    if has_version_req and env_ver < 2:
        parts = list(dir_notes)
        if missing_keys_no_ver:
            parts.append(f"Schluessel aus .env.example fehlen in .env: {', '.join(missing_keys_no_ver)}"
                         " (es gilt der Standard im Code)")
        parts.append(
            "ARGUS_ENV_VERSION fehlt oder ist kleiner als 2. Naechster Schritt: python setup/init_stack.py ausfuehren."
        )
        return DoctorResult("WARNUNG", ".env", "; ".join(parts))

    if missing_keys or dir_notes:
        parts = list(dir_notes)
        if missing_keys:
            parts.append(f"Schluessel aus .env.example fehlen in .env: {', '.join(missing_keys)}"
                         " (es gilt der Standard im Code)")
        return DoctorResult("WARNUNG", ".env", "; ".join(parts) + ".")

    return DoctorResult("OK", ".env", ".env vorhanden, alle Schluessel und ARGUS_*_DIR-Ordner gueltig.")


def _is_dir_writable_for_ugid(path: Path, uid: int, gid: int) -> bool:
    """Prueft lesend anhand der Dateirechte, ob (uid, gid) in <path> schreiben darf."""
    if not path.is_dir():
        return False
    if os.name == "nt":
        return os.access(path, os.W_OK)
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if uid == 0:
        return True
    if st.st_uid == uid:
        return bool(mode & stat.S_IWUSR) and bool(mode & stat.S_IXUSR)
    if st.st_gid == gid:
        return bool(mode & stat.S_IWGRP) and bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IWOTH) and bool(mode & stat.S_IXOTH)


def doctor_check_secrets_and_dirs(repo_root: Path) -> DoctorResult:
    """Prueft API_Tokens/ (Vorhandensein und Rechte unter Linux) sowie die Ordner
    aus Punkt 2 auf Vorhandensein und Beschreibbarkeit fuer ARGUS_UID."""
    init_mod = _load_init_stack_module(repo_root)
    compose_path = repo_root / "docker-compose.yml"
    mounted: set[str] = set()
    if compose_path.exists():
        mounted |= set(
            re.findall(r"\./API_Tokens/([A-Za-z0-9_.]+):", compose_path.read_text(encoding="utf-8"))
        )
    if not mounted:
        mounted = (
            {init_mod.FERNET_KEY_FILE}
            | {g[0] for g in init_mod.GENERATED}
            | set(init_mod.OPTIONAL_PLACEHOLDERS)
        )

    tokens_dir = repo_root / "API_Tokens"
    if not tokens_dir.is_dir():
        return DoctorResult(
            "FEHLER",
            "API_Tokens & Ordner",
            "Ordner API_Tokens/ fehlt. Naechster Schritt: python setup/init_stack.py ausfuehren.",
        )

    errors: list[str] = []
    if os.name != "nt":
        dir_mode = stat.S_IMODE(tokens_dir.stat().st_mode)
        if dir_mode != 0o700:
            errors.append(f"API_Tokens/ hat Rechte {oct(dir_mode)} statt 0o700")

    required_non_empty = {init_mod.FERNET_KEY_FILE} | {g[0] for g in init_mod.GENERATED}
    for name in sorted(mounted):
        p = tokens_dir / name
        if not p.is_file():
            errors.append(f"API_Tokens/{name} fehlt")
            continue
        if name in required_non_empty and p.stat().st_size == 0:
            errors.append(f"API_Tokens/{name} ist leer")
        if os.name != "nt":
            f_mode = stat.S_IMODE(p.stat().st_mode)
            if f_mode != 0o644:
                errors.append(f"API_Tokens/{name} hat Rechte {oct(f_mode)} statt 0o644")

    env_path = repo_root / ".env"
    env_data = (
        init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
        if env_path.exists()
        else {}
    )
    default_uid, default_gid = init_mod.resolve_linux_ugid() if os.name != "nt" else (1000, 1000)
    try:
        argus_uid = int(env_data.get("ARGUS_UID") or default_uid)
    except ValueError:
        argus_uid = default_uid
    try:
        argus_gid = int(env_data.get("ARGUS_GID") or default_gid)
    except ValueError:
        argus_gid = default_gid

    ws_str = _effective_dir(repo_root, env_data, "ARGUS_WORKSPACE_DIR")[0]
    evals_str = _effective_dir(repo_root, env_data, "ARGUS_EVALS_DIR")[0]
    models_str = _effective_dir(repo_root, env_data, "ARGUS_MODELS_DIR")[0]

    check_dirs: list[tuple[str, Path, bool]] = []
    if ws_str and init_mod._usable_here(ws_str):
        ws_p = Path(ws_str)
        check_dirs.append(("ARGUS_WORKSPACE_DIR", ws_p, True))
        check_dirs.append(("ARGUS_WORKSPACE_DIR/Uploads", ws_p / "Uploads", True))
    else:
        errors.append("ARGUS_WORKSPACE_DIR nicht nutzbar")

    if evals_str and init_mod._usable_here(evals_str):
        check_dirs.append(("ARGUS_EVALS_DIR", Path(evals_str), True))
    else:
        errors.append("ARGUS_EVALS_DIR nicht nutzbar")

    if models_str and init_mod._usable_here(models_str):
        check_dirs.append(("ARGUS_MODELS_DIR", Path(models_str), False))
    else:
        errors.append("ARGUS_MODELS_DIR nicht nutzbar")

    for label, d_path, must_write in check_dirs:
        if not d_path.is_dir():
            errors.append(f"{label} fehlt")
        elif must_write and not _is_dir_writable_for_ugid(d_path, argus_uid, argus_gid):
            errors.append(f"{label} ist fuer UID {argus_uid} nicht beschreibbar")

    if errors:
        return DoctorResult(
            "FEHLER",
            "API_Tokens & Ordner",
            "; ".join(errors) + ". Naechster Schritt: python setup/init_stack.py ausfuehren.",
        )

    return DoctorResult("OK", "API_Tokens & Ordner", "Alle Geheimnis-Dateien und Laufzeitordner vorhanden und Rechte stimmen.")


def doctor_check_models(models_dir: Path, models_spec_path: Path, verify_sha256: bool = False) -> DoctorResult:
    """Prueft Modelle aus setup/models.json auf Vorhandensein, Groesse und optional SHA-256."""
    if not models_spec_path.exists():
        return DoctorResult("FEHLER", "Modelle", f"{models_spec_path} fehlt.")
    entries = load_models_spec(models_spec_path)
    missing: list[str] = []
    wrong_size: list[str] = []
    wrong_hash: list[str] = []

    for item in entries:
        target = models_dir / item["target"]
        if not target.is_file():
            missing.append(item["target"])
            continue
        if target.stat().st_size != item["size"]:
            wrong_size.append(item["target"])
            continue
        if verify_sha256:
            if not verify_file_sha256(target, item["sha256"], total_size=item["size"], show_progress=False):
                wrong_hash.append(item["target"])

    problems: list[str] = []
    if missing:
        problems.append(f"fehlend: {', '.join(missing)}")
    if wrong_size:
        problems.append(f"Groesse weicht ab: {', '.join(wrong_size)}")
    if wrong_hash:
        problems.append(f"SHA-256 falsch: {', '.join(wrong_hash)}")

    if problems:
        return DoctorResult(
            "FEHLER",
            "Modelle",
            "; ".join(problems) + ". Naechster Schritt: python setup/install.py --models-only.",
        )

    suffix = "Groesse und SHA-256 geprueft" if verify_sha256 else "Groesse geprueft"
    return DoctorResult("OK", "Modelle", f"Alle {len(entries)} Modelldateien vorhanden ({suffix}).")


def doctor_check_networks(repo_root: Path) -> DoctorResult:
    """Prueft die Netz-Abschottung per 'docker compose config --format json' und setup/check_networks.py."""
    net_mod = _load_check_networks_module(repo_root)
    env_path = repo_root / ".env"

    # Falls noch keine .env existiert, eine temporaer leere Datei fuer die Compose-Aufloesung nutzen.
    temp_env: Path | None = None
    try:
        cmd = ["docker", "compose"]
        if not env_path.exists():
            fd, tmp_name = tempfile.mkstemp(prefix="argus-empty-env-", suffix=".env")
            os.close(fd)
            temp_env = Path(tmp_name)
            cmd.extend(["--env-file", str(temp_env)])
        cmd.extend(["config", "--format", "json"])

        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not res.stdout.strip():
            err_line = (res.stderr or "").strip().splitlines()
            short_err = err_line[0] if err_line else "docker compose config fehlgeschlagen"
            return DoctorResult("FEHLER", "Netz-Abschottung", short_err)

        cfg = json.loads(res.stdout)
        errors = net_mod.check(cfg)
        if errors:
            return DoctorResult("FEHLER", "Netz-Abschottung", "; ".join(errors))
        return DoctorResult(
            "OK",
            "Netz-Abschottung",
            f"{len(cfg.get('services') or {})} Dienste geprueft, keine Abweichung.",
        )
    except Exception as exc:
        return DoctorResult("FEHLER", "Netz-Abschottung", f"Pruefung fehlgeschlagen: {exc}")
    finally:
        if temp_env is not None:
            temp_env.unlink(missing_ok=True)


PROFILE_SERVICES: dict[str, tuple[str, ...]] = {
    "voice": ("stt-service", "tts-service"),
    "tracing": ("phoenix",),
    "web": ("searxng", "local-proxy"),
    "proxy": ("caddy",),
}


def doctor_check_profiles(
    repo_root_or_env: Path | dict[str, str],
    env_data: dict[str, str] | None = None,
) -> DoctorResult:
    """Prueft COMPOSE_PROFILES in der .env und meldet die aktiven Profile."""
    if isinstance(repo_root_or_env, dict):
        env_data = repo_root_or_env
    else:
        repo_root = Path(repo_root_or_env)
        if env_data is None:
            init_mod = _load_init_stack_module(repo_root)
            env_path = repo_root / ".env"
            env_data = (
                init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
                if env_path.exists()
                else {}
            )

    if "COMPOSE_PROFILES" not in env_data:
        return DoctorResult(
            "WARNUNG",
            "Profile",
            "COMPOSE_PROFILES fehlt in der .env (Sprache und Ablaufverfolgung sind damit aus). "
            "Naechster Schritt: python setup/init_stack.py ausfuehren oder COMPOSE_PROFILES in der .env setzen.",
        )

    profiles = [p.strip() for p in (env_data.get("COMPOSE_PROFILES") or "").split(",") if p.strip()]
    if not profiles:
        return DoctorResult("OK", "Profile", "Keine optionalen Profile aktiv (nur Kerndienste).")
    return DoctorResult("OK", "Profile", f"Aktive Profile: {', '.join(profiles)}.")


def doctor_check_stack(
    repo_root: Path,
    healthz_url: str = "http://127.0.0.1:7860/healthz",
    env_data: dict[str, str] | None = None,
) -> DoctorResult:
    """Prueft den Laufzustand der Container (docker compose ps) und /healthz auf 127.0.0.1:7860."""
    if env_data is None:
        env_path = repo_root / ".env"
        if env_path.exists():
            try:
                init_mod = _load_init_stack_module(repo_root)
                env_data = init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
            except Exception:
                env_data = {}
        else:
            env_data = {}

    active_profiles = {
        p.strip() for p in (env_data.get("COMPOSE_PROFILES") or "").split(",") if p.strip()
    }
    inactive_optional: set[str] = set()
    expected_optional: list[str] = []
    for prof, svcs in PROFILE_SERVICES.items():
        if prof in active_profiles:
            if prof in ("voice", "tracing"):
                expected_optional.extend(svcs)
        else:
            inactive_optional.update(svcs)

    try:
        res = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
    except Exception as exc:
        return DoctorResult("FEHLER", "Stapel-Status", f"docker compose ps fehlgeschlagen: {exc}")

    items: list[dict] = []
    if res.returncode == 0 and res.stdout.strip():
        text = res.stdout.strip()
        try:
            if text.startswith("["):
                items = json.loads(text)
            else:
                for line in text.splitlines():
                    if line.strip():
                        items.append(json.loads(line.strip()))
        except Exception:
            items = []

    # Optionale Container eines abgeschalteten Profils nicht als Fehler werten,
    # falls sie gestoppt in 'docker compose ps' stehen oder fehlen.
    items = [
        it for it in items
        if not (
            (it.get("Service") or it.get("Name", "")) in inactive_optional
            and it.get("State", "").lower() != "running"
        )
    ]

    healthy = check_healthz(healthz_url)
    if not items:
        if healthy:
            return DoctorResult("OK", "Stapel-Status", "/healthz auf 127.0.0.1:7860 antwortet mit HTTP 200.")
        return DoctorResult(
            "WARNUNG",
            "Stapel-Status",
            "Container-Stapel laeuft derzeit nicht. Naechster Schritt: docker compose up -d.",
        )

    present_services = {(it.get("Service") or it.get("Name", "")) for it in items}
    missing_optional = [s for s in expected_optional if s not in present_services]
    not_running = [
        (it.get("Service") or it.get("Name", "dienst"))
        for it in items
        if it.get("State", "").lower() != "running"
    ]
    unhealthy = [
        (it.get("Service") or it.get("Name", "dienst"))
        for it in items
        if it.get("Health", "").lower() == "unhealthy"
    ]
    if missing_optional or not_running or unhealthy:
        problems = []
        if missing_optional:
            problems.append(f"fehlend: {', '.join(missing_optional)}")
        if not_running:
            problems.append(f"nicht laufend: {', '.join(not_running)}")
        if unhealthy:
            problems.append(f"ungesund: {', '.join(unhealthy)}")
        return DoctorResult(
            "FEHLER",
            "Stapel-Status",
            "; ".join(problems) + ". Naechster Schritt: docker compose logs pruefen.",
        )

    if healthy:
        return DoctorResult(
            "OK",
            "Stapel-Status",
            f"{len(items)} Dienste laufen, /healthz auf 127.0.0.1:7860 antwortet.",
        )

    starting = [
        (it.get("Service") or it.get("Name", "dienst"))
        for it in items
        if it.get("Health", "").lower() == "starting"
    ]
    if starting:
        return DoctorResult(
            "WARNUNG",
            "Stapel-Status",
            f"Dienste starten noch ({', '.join(starting)}); /healthz antwortet noch nicht.",
        )
    return DoctorResult(
        "FEHLER",
        "Stapel-Status",
        "Container laufen, aber /healthz auf 127.0.0.1:7860 antwortet nicht. Naechster Schritt: docker compose logs rag-backend.",
    )


def doctor_check_switches(env_data: dict[str, str]) -> list[DoctorResult]:
    """Prueft sicherheitsrelevante Schalter in der .env und liefert Hinweise als WARNUNG.
    Gibt niemals Werte von Geheimnissen aus."""
    warnings: list[DoctorResult] = []

    if env_data.get("CLOUD_AGENTS_ENABLED", "").strip().lower() == "true":
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter CLOUD_AGENTS_ENABLED",
            "CLOUD_AGENTS_ENABLED=true: Cloud-Helfer sind eingeschaltet (Anfragen koennen geschwaerzt an externe Modell-APIs gehen).",
        ))
    if env_data.get("CLOUD_MISSION_AUTO_APPROVE", "").strip().lower() == "true":
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter CLOUD_MISSION_AUTO_APPROVE",
            "CLOUD_MISSION_AUTO_APPROVE=true: Cloud-Missionen und cloud_ask laufen ohne Einzelfreigabe.",
        ))
    if not env_data.get("DASHBOARD_SETUP_TOKEN", "").strip():
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter DASHBOARD_SETUP_TOKEN",
            "DASHBOARD_SETUP_TOKEN ist leer: Beim Erstlauf kann jeder, der die Seite zuerst erreicht, das Administrator-Konto anlegen.",
        ))
    if env_data.get("EGRESS_AFTER_SENSITIVE", "").strip().lower() == "allow":
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter EGRESS_AFTER_SENSITIVE",
            "EGRESS_AFTER_SENSITIVE=allow: Nach dem Lesen sensibler Daten bleibt der Internetzugang im Gespraech offen.",
        ))
    if env_data.get("WEB_INJECTION_GUARD", "true").strip().lower() != "true":
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter WEB_INJECTION_GUARD",
            "WEB_INJECTION_GUARD ist nicht 'true': Die Pruefung abgerufener Webseiten auf Prompt-Injection ist abgeschaltet.",
        ))
    if env_data.get("API_DOCS", "").strip().lower() == "true":
        warnings.append(DoctorResult(
            "WARNUNG",
            "Schalter API_DOCS",
            "API_DOCS=true: Die Routenbeschreibung (/docs, /openapi.json) ist ohne Anmeldung abrufbar.",
        ))

    if not warnings:
        return [DoctorResult("OK", "Sicherheitsschalter", "Keine risikobehafteten Schalter aktiv.")]
    return warnings


def selinux_enforcing(enforce_file: Path = Path("/sys/fs/selinux/enforce")) -> bool:
    """True, wenn SELinux unter Linux im Modus enforcing laeuft."""
    if os.name == "nt":
        return False
    try:
        if enforce_file.exists() and enforce_file.read_text(encoding="utf-8").strip() == "1":
            return True
    except OSError:
        pass
    if shutil.which("getenforce"):
        rc, out, _ = run_command(["getenforce"])
        if rc == 0 and out.strip().lower() == "enforcing":
            return True
    return False


#--- Mit :z beschriftet Docker den eingebundenen Ordner samt Inhalt um. Fuer System-
#--- ordner oder ein ganzes Home-Verzeichnis waere das schaedlich: ~/.ssh verloere sein
#--- Label, und sshd laese die Schluessel nicht mehr.
_RELABEL_FORBIDDEN = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/media", "/mnt",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
}


def relabel_blockers(repo_root: Path, env_data: dict[str, str],
                     home: Path | None = None) -> list[str]:
    """Eingebundene Ordner, die :z nicht umbeschriften darf (Systemordner, ein ganzes
    Home-Verzeichnis). Leer = unbedenklich."""
    home = (home or Path.home()).resolve(strict=False)
    data_dir, tls_dir = proxy_dirs(repo_root, env_data)
    candidates = [
        ("ARGUS_WORKSPACE_DIR", _effective_dir(repo_root, env_data, "ARGUS_WORKSPACE_DIR")[0]),
        ("ARGUS_EVALS_DIR", _effective_dir(repo_root, env_data, "ARGUS_EVALS_DIR")[0]),
        ("ARGUS_MODELS_DIR", _effective_dir(repo_root, env_data, "ARGUS_MODELS_DIR")[0]),
        ("ARGUS_PROXY_DATA_DIR", str(data_dir)),
        ("ARGUS_TLS_DIR", str(tls_dir)),
    ]
    out = []
    for key, raw in candidates:
        if not raw:
            continue
        p = Path(raw).resolve(strict=False)
        posix = p.as_posix()
        if posix in _RELABEL_FORBIDDEN or p == home or p.parent.as_posix() == "/home":
            out.append(f"{key}={posix}")
    return out


def configure_selinux_label(repo_root: Path, non_interactive: bool = False) -> bool:
    """Setzt unter SELinux (enforcing) ARGUS_MOUNT_LABEL=,z in der .env, damit die
    Container die eingebundenen Ordner lesen duerfen. Verweigert das fuer System- und
    ganze Home-Ordner."""
    init_mod = _load_init_stack_module(repo_root)
    env_path = repo_root / ".env"
    env_data = init_mod.parse_env(env_path.read_bytes().decode("utf-8")) if env_path.exists() else {}
    if "z" in (env_data.get("ARGUS_MOUNT_LABEL") or "").lower():
        return True
    print("SELinux laeuft im Modus enforcing. Ohne Label duerfen die Container die")
    print("eingebundenen Ordner nicht lesen.")
    blockers = relabel_blockers(repo_root, env_data)
    if blockers:
        print("  Das Label :z wuerde ganze System- oder Home-Ordner umbeschriften:")
        for b in blockers:
            print(f"    {b}")
        print("  Bitte Unterordner waehlen (etwa ~/argus/workspace) und die Installation erneut starten.")
        return False
    if not prompt_bool("Sollen die Einbindungen das SELinux-Label :z bekommen (empfohlen)?",
                       default=True, non_interactive=non_interactive):
        print("  Ohne Label starten die Dienste nicht; spaeter ARGUS_MOUNT_LABEL=,z in die .env eintragen.")
        return False
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    init_mod.write_env_file(env_path, template_text, {"ARGUS_MOUNT_LABEL": ",z"})
    print("  ARGUS_MOUNT_LABEL=,z gesetzt.")
    return True


def doctor_check_selinux(enforce_file: Path = Path("/sys/fs/selinux/enforce"),
                         repo_root: Path | None = None,
                         env_data: dict[str, str] | None = None) -> DoctorResult:
    """Prueft unter Linux SELinux und, wenn es erzwingt, das Label der Einbindungen."""
    if os.name == "nt":
        return DoctorResult("OK", "SELinux", "Unter Windows nicht zutreffend.")
    if not selinux_enforcing(enforce_file):
        return DoctorResult("OK", "SELinux", "SELinux ist nicht im Modus enforcing.")

    env_data = env_data or {}
    if "z" not in (env_data.get("ARGUS_MOUNT_LABEL") or "").lower():
        return DoctorResult(
            "WARNUNG",
            "SELinux",
            "SELinux erzwingt, die Einbindungen haben kein Label. Naechster Schritt: "
            "ARGUS_MOUNT_LABEL=,z in die .env (./install.sh fragt danach).",
        )
    blockers = relabel_blockers(repo_root, env_data) if repo_root is not None else []
    if blockers:
        return DoctorResult(
            "FEHLER",
            "SELinux",
            "ARGUS_MOUNT_LABEL=,z wuerde System- oder ganze Home-Ordner umbeschriften: "
            + ", ".join(blockers) + ". Naechster Schritt: Unterordner waehlen.",
        )
    return DoctorResult("OK", "SELinux", "SELinux erzwingt, die Einbindungen tragen das Label :z.")


def proxy_dirs(repo_root: Path, env_data: dict[str, str]) -> tuple[Path, Path]:
    """Datenordner (ARGUS_PROXY_DATA_DIR) und Zertifikatsordner (ARGUS_TLS_DIR) des
    TLS-Proxys; relative Angaben gelten wie in compose vom Projektordner aus."""
    def _resolve(key: str, default: str) -> Path:
        raw = (env_data.get(key) or "").strip() or default
        p = Path(raw)
        return p if p.is_absolute() else repo_root / p
    return _resolve("ARGUS_PROXY_DATA_DIR", "proxy/data"), _resolve("ARGUS_TLS_DIR", "proxy/certs")


def _argus_ugid(env_data: dict[str, str]) -> tuple[int, int]:
    """ARGUS_UID/ARGUS_GID aus der .env, sonst wie compose 1000:1000."""
    def _int(key: str) -> int:
        try:
            return int(env_data.get(key) or 1000)
        except ValueError:
            return 1000
    return _int("ARGUS_UID"), _int("ARGUS_GID")


def _is_readable_for_ugid(path: Path, uid: int, gid: int, directory: bool = False) -> bool:
    """Prueft lesend anhand der Dateirechte, ob (uid, gid) <path> lesen (bzw. als
    Ordner betreten und auflisten) darf. Unter Windows entscheidet Docker Desktop."""
    if os.name == "nt":
        return path.exists()
    try:
        st = path.stat()
    except OSError:
        return False
    mode = stat.S_IMODE(st.st_mode)
    need = (stat.S_IRUSR | stat.S_IXUSR) if directory else stat.S_IRUSR
    if st.st_uid == uid:
        return mode & need == need
    if st.st_gid == gid:
        return mode & (need >> 3) == need >> 3
    return mode & (need >> 6) == need >> 6


def doctor_check_proxy(
    repo_root_or_env: Path | dict[str, str],
    env_data: dict[str, str] | None = None,
) -> DoctorResult:
    """Prueft die TLS-Proxy-Einstellungen (Profil 'proxy' und ARGUS_TRUSTED_PROXIES)."""
    if isinstance(repo_root_or_env, dict):
        env_data = repo_root_or_env
        repo_root = Path(__file__).resolve().parent.parent
    else:
        repo_root = Path(repo_root_or_env)
        if env_data is None:
            init_mod = _load_init_stack_module(repo_root)
            env_path = repo_root / ".env"
            env_data = (
                init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
                if env_path.exists()
                else {}
            )

    profiles = {
        p.strip() for p in (env_data.get("COMPOSE_PROFILES") or "").split(",") if p.strip()
    }
    proxy_active = "proxy" in profiles
    trusted_proxies = (env_data.get("ARGUS_TRUSTED_PROXIES") or "").strip()

    if not proxy_active:
        if trusted_proxies:
            return DoctorResult(
                "WARNUNG",
                "TLS-Proxy",
                "ARGUS_TRUSTED_PROXIES ist ohne Profil 'proxy' gesetzt (dann muss der eigene Proxy darin stehen).",
            )
        return DoctorResult("OK", "TLS-Proxy", "Profil 'proxy' nicht aktiv (lokaler Betrieb).")

    errors: list[str] = []
    uid, gid = _argus_ugid(env_data)
    data_dir, tls_dir = proxy_dirs(repo_root, env_data)
    domain = (env_data.get("ARGUS_DOMAIN") or "").strip()
    if not domain:
        errors.append("ARGUS_DOMAIN nicht gesetzt")
    else:
        #--- Ohne diese beiden Eintraege antwortet das Backend auf Anfragen ueber den
        #--- Proxy mit 400 (Host-Pruefung) bzw. die Anmeldung scheitert am CSRF-Schutz.
        hosts = {
            h.strip().lower()
            for h in (env_data.get("TRUSTED_HOSTS") or "127.0.0.1,localhost,rag-backend").split(",")
        }
        if domain.lower() not in hosts and "*" not in hosts:
            errors.append(f"{domain} fehlt in TRUSTED_HOSTS")
        origins = [
            urllib.parse.urlsplit(o.strip())
            for o in (env_data.get("DASHBOARD_ORIGINS") or "").split(",") if o.strip()
        ]
        if not any(o.scheme == "https" and (o.hostname or "") == domain.lower() for o in origins):
            errors.append(f"https://{domain} fehlt in DASHBOARD_ORIGINS")

    if not data_dir.is_dir():
        errors.append(f"Datenordner des Proxys {data_dir} fehlt")
    elif not _is_dir_writable_for_ugid(data_dir, uid, gid):
        errors.append(f"Datenordner des Proxys {data_dir} ist fuer UID {uid} nicht beschreibbar")

    tls_mode = (env_data.get("ARGUS_TLS_MODE") or "").strip()
    if tls_mode not in VALID_TLS_MODES:
        errors.append(
            f"ARGUS_TLS_MODE '{tls_mode}' ungueltig (erlaubt: {', '.join(VALID_TLS_MODES)})"
        )
    elif tls_mode == "files":
        missing_certs = [name for name in ("cert.pem", "key.pem") if not (tls_dir / name).is_file()]
        if missing_certs:
            errors.append(f"Zertifikatsdateien fuer ARGUS_TLS_MODE=files fehlen ({', '.join(missing_certs)})")
        else:
            unreadable = [
                name for name in ("cert.pem", "key.pem")
                if not _is_readable_for_ugid(tls_dir / name, uid, gid)
            ]
            if unreadable or not _is_readable_for_ugid(tls_dir, uid, gid, directory=True):
                errors.append(
                    f"{tls_dir} bzw. {', '.join(unreadable) or 'der Ordner'} ist fuer UID {uid} "
                    "nicht lesbar (Caddy laeuft als ARGUS_UID)"
                )

    if not trusted_proxies:
        errors.append("ARGUS_TRUSTED_PROXIES nicht gesetzt")

    if errors:
        return DoctorResult("FEHLER", "TLS-Proxy", "; ".join(errors) + ".")

    return DoctorResult(
        "OK",
        "TLS-Proxy",
        f"Profil 'proxy' aktiv (Domain: {domain}, TLS-Modus: {tls_mode}).",
    )


def run_doctor(repo_root: Path, verify_models: bool = False) -> tuple[int, list[DoctorResult]]:
    """Fuehrt alle Diagnose-Pruefungen lesend aus und gibt eine Zeile je Pruefung aus."""
    init_mod = _load_init_stack_module(repo_root)
    env_path = repo_root / ".env"
    env_data = (
        init_mod.parse_env(env_path.read_bytes().decode("utf-8"))
        if env_path.exists()
        else {}
    )
    #--- Derselbe Ordner, den Compose einbindet (Schluessel oder Standardwert der
    #--- Compose-Datei); unter Linux ohne Schluessel die Vorgabe der Installation.
    models_dir = Path(_effective_dir(repo_root, env_data, "ARGUS_MODELS_DIR")[0]
                      or os.path.expanduser("~/argus/models"))
    models_spec_path = repo_root / "setup" / "models.json"

    results: list[DoctorResult] = [
        doctor_check_docker(),
        doctor_check_gpu(),
        doctor_check_ram(),
        doctor_check_disk_space(models_dir, models_spec_path),
        doctor_check_env(repo_root),
        doctor_check_profiles(repo_root, env_data),
        doctor_check_secrets_and_dirs(repo_root),
        doctor_check_models(models_dir, models_spec_path, verify_sha256=verify_models),
        doctor_check_networks(repo_root),
        doctor_check_proxy(repo_root, env_data),
        doctor_check_stack(repo_root, env_data=env_data),
    ]
    results.extend(doctor_check_switches(env_data))
    if os.name != "nt":
        results.append(doctor_check_selinux(repo_root=repo_root, env_data=env_data))

    for r in results:
        print(r.format_line())

    has_error = any(r.status == "FEHLER" for r in results)
    return (1 if has_error else 0), results


# =============================================================================
# Passwort zuruecksetzen (reset-password <konto>)
# =============================================================================

def run_reset_password(repo_root: Path, account: str) -> int:
    """Fragt das neue Passwort zweimal verdeckt ab und uebergibt es per Standardeingabe
    an rag_backend.admin_cli im laufenden Container rag-backend."""
    name = (account or "").strip()
    if not name:
        print("Fehler: Bitte einen Kontonamen angeben.", file=sys.stderr)
        return 1

    try:
        pw1 = getpass.getpass(f"Neues Passwort fuer '{name}': ")
        pw2 = getpass.getpass("Passwort wiederholen: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen.", file=sys.stderr)
        return 1

    if pw1 != pw2:
        print("Fehler: Die eingegebenen Passwoerter stimmen nicht ueberein.", file=sys.stderr)
        return 1
    if len(pw1) < 8:
        print("Fehler: Das Passwort muss mindestens 8 Zeichen lang sein.", file=sys.stderr)
        return 1

    cmd = [
        "docker", "compose", "exec", "-T", "rag-backend",
        "python", "-m", "rag_backend.admin_cli", "reset-password", name,
    ]
    res = subprocess.run(
        cmd,
        input=pw1 + "\n",
        text=True,
        cwd=repo_root,
        check=False,
    )
    return res.returncode


# =============================================================================
# Hauptablauf
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gefuehrte Installation fuer Argus.")
    parser.add_argument("--yes", "-y", action="store_true", help="Alle Fragen mit Vorgabe/Ja beantworten.")
    parser.add_argument("--workspace", type=str, default=None, help="Arbeitsordner (ARGUS_WORKSPACE_DIR).")
    parser.add_argument("--models", type=str, default=None, help="Modellordner (ARGUS_MODELS_DIR).")
    parser.add_argument("--server-url", type=str, default=None, help="Server-Adresse (z. B. https://argus.example.org).")
    parser.add_argument("--proxy", dest="use_proxy", action="store_true", default=None,
                        help="Mitgelieferten TLS-Proxy (Profil 'proxy') aktivieren.")
    parser.add_argument("--no-proxy", dest="use_proxy", action="store_false", default=None,
                        help="Mitgelieferten TLS-Proxy nicht aktivieren.")
    parser.add_argument("--tls-mode", type=str, default=None,
                        help="TLS-Modus fuer den Proxy (internal, acme, files; Standard: internal).")
    parser.add_argument("--voice", dest="use_voice", action="store_true", default=None,
                        help="Spracheingabe und Vorlesen (Profil 'voice') aktivieren.")
    parser.add_argument("--no-voice", dest="use_voice", action="store_false", default=None,
                        help="Spracheingabe und Vorlesen (Profil 'voice') abschalten.")
    parser.add_argument("--tracing", dest="use_tracing", action="store_true", default=None,
                        help="Ablaufverfolgung mit Phoenix (Profil 'tracing') aktivieren.")
    parser.add_argument("--no-tracing", dest="use_tracing", action="store_false", default=None,
                        help="Ablaufverfolgung mit Phoenix (Profil 'tracing') abschalten.")
    parser.add_argument("--web", dest="use_web", action="store_true", default=None,
                        help="Websuche und Seitenabruf aus dem Internet (Profil 'web') aktivieren.")
    parser.add_argument("--no-web", dest="use_web", action="store_false", default=None,
                        help="Websuche und Seitenabruf aus dem Internet (Profil 'web') abschalten.")
    parser.add_argument("--no-models", action="store_true", help="Modelle nicht laden.")
    parser.add_argument("--no-start", action="store_true", help="Stapel nicht starten.")
    parser.add_argument("--models-only", action="store_true", help="Nur Modelle pruefen und laden.")

    subparsers = parser.add_subparsers(dest="subcommand")
    doc_parser = subparsers.add_parser("doctor", help="Installation und Umgebung lesend pruefen.")
    doc_parser.add_argument("--verify-models", action="store_true", help="Modelldateien zusaetzlich per SHA-256 pruefen.")
    doc_parser.add_argument("--stack", type=str, default=None, help="Pfad zur Installation (Standard: dieses Repo).")

    rp_parser = subparsers.add_parser("reset-password", help="Passwort eines Kontos im Backend-Container zuruecksetzen.")
    rp_parser.add_argument("account", help="Kontoname, dessen Passwort zurueckgesetzt werden soll.")

    bd_parser = subparsers.add_parser("bundle", help="Offline-Paket bauen: Images, Quellstand, auf Wunsch Modelle.")
    bd_parser.add_argument("--out", type=str, default=None, help="Zielordner (Standard: ./argus-bundle-<version>).")
    bd_parser.add_argument("--version", dest="bundle_version", type=str, default=None,
                           help="Version der Images (Standard: Datum und Commit).")
    bd_parser.add_argument("--with-models", action="store_true", help="Modelldateien mit ins Paket legen.")
    bd_parser.add_argument("--services", type=str, default=None,
                           help="Nur diese Dienste (Komma-getrennt), etwa zum Ausprobieren.")

    #--- Fertige Images statt eigenem Bau.
    parser.add_argument("--bundle", type=str, default=None,
                        help="Offline-Paket (Ordner von 'bundle'): Images und Modelle daraus, ohne Internet.")
    parser.add_argument("--image-prefix", type=str, default=None,
                        help="Fertige Images aus einer Registry, etwa ghcr.io/firma/argus.")
    parser.add_argument("--image-version", type=str, default=None,
                        help="Version der Registry-Images (mit --image-prefix).")

    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent

    if args.subcommand == "doctor":
        target_root = Path(args.stack).resolve() if args.stack else repo_root
        rc, _ = run_doctor(target_root, verify_models=args.verify_models)
        return rc

    if args.subcommand == "reset-password":
        return run_reset_password(repo_root, args.account)

    if args.subcommand == "bundle":
        return run_bundle(repo_root, args.out, args.bundle_version, args.with_models, args.services)

    bundle_dir = Path(args.bundle).resolve() if args.bundle else None
    if bundle_dir is not None and args.image_prefix:
        print("Fehler: --bundle und --image-prefix schliessen sich aus.")
        return 1
    if args.image_prefix and not args.image_version:
        print("Fehler: --image-prefix braucht --image-version.")
        return 1
    if bundle_dir is not None:
        try:
            load_bundle_manifest(bundle_dir)
        except ValueError as exc:
            print(f"Fehler in --bundle: {exc}")
            return 1

    if args.tls_mode is not None and args.tls_mode not in VALID_TLS_MODES:
        print(f"Fehler in --tls-mode: '{args.tls_mode}' ist ungueltig (erlaubt: {', '.join(VALID_TLS_MODES)}).")
        return 1

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
    if not warn_if_sudo(non_interactive=args.yes):
        print("Abgebrochen.")
        return 1
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
    use_proxy = False
    tls_mode = "internal"
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

    if server_url:
        if args.use_proxy is not None:
            use_proxy = args.use_proxy
        else:
            use_proxy = prompt_bool("Soll der mitgelieferte TLS-Proxy (Caddy) genutzt werden?",
                                    default=True, non_interactive=args.yes)
        if use_proxy:
            if args.tls_mode is not None:
                tls_mode = args.tls_mode
            else:
                tls_mode = prompt_choice(
                    "TLS-Modus [internal = eigene CA, acme = Let's Encrypt, files = eigenes Zertifikat]",
                    choices=list(VALID_TLS_MODES),
                    default="internal",
                    non_interactive=args.yes,
                )

    if args.use_voice is not None:
        use_voice = args.use_voice
    else:
        use_voice = prompt_bool(
            "Spracheingabe und Vorlesen? Der erste Bau laedt dafuer 4,4 GB.",
            default=True,
            non_interactive=args.yes,
        )

    if args.use_tracing is not None:
        use_tracing = args.use_tracing
    else:
        use_tracing = prompt_bool(
            "Ablaufverfolgung mit Phoenix, nur lokal?",
            default=True,
            non_interactive=args.yes,
        )

    if args.use_web is not None:
        use_web = args.use_web
    else:
        use_web = prompt_bool(
            "Websuche und Seitenabruf aus dem Internet? Ohne geht von Argus nichts ins Internet (ausser dem Cloud-Modul, wenn es an ist).",
            default=True,
            non_interactive=args.yes,
        )
    print("")

    # -------------------------------------------------------------------------
    # Schritt 4: Geheimnisse, .env und Laufzeit-Ordner anlegen
    # -------------------------------------------------------------------------
    print("[4/7] Erzeuge Geheimnisse und .env...")
    if bundle_dir is not None:
        venv_python = ensure_setup_venv(repo_root, wheels_dir=bundle_dir / "wheels")
    else:
        venv_python = ensure_setup_venv(repo_root)
    run_init_stack(repo_root, venv_python, workspace=workspace_dir, models=models_dir)
    ensure_installation_dirs(repo_root, workspace_dir=workspace_dir, models_dir=models_dir)

    if server_url:
        update_server_env(repo_root, server_url, use_proxy=use_proxy, tls_mode=tls_mode)
    update_compose_profiles(repo_root, use_voice=use_voice, use_tracing=use_tracing, use_web=use_web)
    if selinux_enforcing():
        configure_selinux_label(repo_root, non_interactive=args.yes)
    try:
        if bundle_dir is not None:
            apply_bundle(repo_root, bundle_dir)
        elif args.image_prefix:
            configure_registry_images(repo_root, args.image_prefix, args.image_version)
            print(f"  Fertige Images aus {args.image_prefix}, Version {args.image_version}.")
    except (RuntimeError, ValueError) as exc:
        print(f"Fehler: {exc}")
        return 1
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
        if bundle_dir is not None:
            copy_models_from_bundle(bundle_dir, Path(models_dir), models_spec_path)
        modelle_ok = _execute_models_step(models_spec_path, models_dir, non_interactive=args.yes)
        print("")

    # -------------------------------------------------------------------------
    # Schritt 6: Container-Stapel bauen und starten
    # -------------------------------------------------------------------------
    print("[6/7] Start des Container-Stapels...")
    if args.no_start:
        print("  Start des Stapels uebersprungen (--no-start).\n")
    else:
        if bundle_dir is not None or args.image_prefix:
            print("Hinweis: Die Images kommen fertig, gebaut wird nichts.")
        elif use_voice:
            print("Hinweis: Der erste Bau dauert etwas, der Sprachdienst laedt dabei 4,4 GB.")
        else:
            print("Hinweis: Der erste Bau dauert etwas.")
        if not modelle_ok:
            print("Achtung: Es fehlen Modelldateien. Ohne sie starten Sprachmodell und")
            print("Dokumentensuche nicht. Erst die Modelle laden: install.cmd --models-only")
        start_now = prompt_bool("Soll der Container-Stapel jetzt gebaut und gestartet werden?",
                                default=modelle_ok, non_interactive=args.yes)
        if start_now:
            #--- Mit fertigen Images nichts bauen: aus dem Offline-Paket ohne Internet,
            #--- aus der Registry vorher holen.
            if args.image_prefix:
                print("Fuehre 'docker compose pull' aus...")
                if subprocess.run(["docker", "compose", "pull"], cwd=repo_root, check=False).returncode != 0:
                    print("Fehler beim Holen der Images aus der Registry.")
                    return 1
            up_cmd = ["docker", "compose", "up", "-d"]
            if bundle_dir is None and not args.image_prefix:
                up_cmd.append("--build")
            print(f"Fuehre '{' '.join(up_cmd)}' aus...")
            c_res = subprocess.run(up_cmd, cwd=repo_root, check=False)
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
        init_mod = _load_init_stack_module(repo_root)
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


# =============================================================================
# Fertige Images: Offline-Paket (bundle) und eigene Registry
# =============================================================================

BUNDLE_MANIFEST = "argus-bundle.json"
BUNDLE_IMAGES = "images.tar"
BUNDLE_OVERRIDE = "docker-compose.bundle.yml"
#--- Ein Paket enthaelt jeden Dienst aller Profile; welche laufen, entscheidet
#--- COMPOSE_PROFILES auf dem Zielrechner.
BUNDLE_PROFILES = ("voice", "tracing", "web", "proxy")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_IMAGE_PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)*")


def _profile_args(profiles=BUNDLE_PROFILES) -> list[str]:
    out: list[str] = []
    for p in profiles:
        out += ["--profile", p]
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_checked(cmd: list[str], cwd: Path, env: dict | None, what: str) -> None:
    print(f"  {what}...")
    res = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"{what} fehlgeschlagen (Exit {res.returncode}).")


def _image_present(ref: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", ref],
                          capture_output=True, check=False).returncode == 0


def default_bundle_version(repo_root: Path) -> str:
    """Datum und Commit, etwa 2026.11.18-d7e5f2f."""
    stamp = time.strftime("%Y.%m.%d")
    rc, out, _ = run_command(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"])
    return f"{stamp}-{out.strip()}" if rc == 0 and out.strip() else stamp


def create_bundle(repo_root: Path, out_dir: Path, version: str, with_models: bool = False,
                  services: list[str] | None = None) -> dict:
    """Baut die eigenen Images, holt die fremden (per Digest festgelegt) und legt alles
    unter einem Namen je Dienst (argus/<dienst>:<version>) in out_dir ab, dazu den
    Quellstand und auf Wunsch die Modelle. Auf dem Zielrechner braucht die Installation
    dann kein Internet: install --bundle <out_dir>.

    Die fremden Images werden umbenannt statt per Digest referenziert: docker save/load
    behaelt Digests nicht mit jedem Image-Speicher. Die Pruefsumme der images.tar sichert
    den Weg vom Bau- zum Zielrechner."""
    if not _VERSION_RE.fullmatch(version or ""):
        raise ValueError("Ungueltige Version (erlaubt: Buchstaben, Ziffern, Punkt, _ und -).")
    env = {**os.environ, "ARGUS_IMAGE_PREFIX": "argus", "ARGUS_VERSION": version,
           "ARGUS_PULL_POLICY": "never"}
    profile_args = _profile_args()
    #--- Die Ausgabe enthaelt die aufgeloeste Umgebung (auch Geheimnisse aus der .env):
    #--- nur im Speicher auswerten, nie ausgeben.
    res = subprocess.run(["docker", "compose", *profile_args, "config", "--format", "json"],
                         cwd=repo_root, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError("docker compose config fehlgeschlagen.")
    all_services = json.loads(res.stdout).get("services") or {}
    names = sorted(services) if services else sorted(all_services)
    unknown = [n for n in names if n not in all_services]
    if unknown:
        raise ValueError(f"Unbekannte Dienste: {', '.join(unknown)}")
    built = [n for n in names if all_services[n].get("build")]
    foreign = [n for n in names if not all_services[n].get("build")]
    targets = {n: f"argus/{n}:{version}" for n in names}

    out_dir.mkdir(parents=True, exist_ok=True)
    if built:
        _run_checked(["docker", "compose", *profile_args, "build", *built], repo_root, env,
                     f"Eigene Images bauen ({', '.join(built)})")
    for n in foreign:
        ref = all_services[n]["image"]
        if not _image_present(ref):
            _run_checked(["docker", "pull", ref], repo_root, env, f"{ref} holen")
        _run_checked(["docker", "tag", ref, targets[n]], repo_root, env, f"{n} als {targets[n]} markieren")
    images_tar = out_dir / BUNDLE_IMAGES
    _run_checked(["docker", "save", "-o", str(images_tar), *sorted(set(targets.values()))],
                 repo_root, env, f"Images nach {images_tar} speichern")

    override = ["# Erzeugt von 'install bundle': Images aus dem Offline-Paket, nie aus einer Registry.",
                "services:"]
    for n in names:
        override += [f"  {n}:", f"    image: {targets[n]}", "    pull_policy: never"]
    (out_dir / BUNDLE_OVERRIDE).write_text("\n".join(override) + "\n", encoding="utf-8")

    git_commit = ""
    if (repo_root / ".git").exists() and shutil.which("git"):
        rc, out, _ = run_command(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
        git_commit = out.strip() if rc == 0 else ""
        _run_checked(["git", "-C", str(repo_root), "archive", "--format=tar",
                      "-o", str(out_dir / "argus-src.tar"), "HEAD"], repo_root, None,
                     "Quellstand packen (argus-src.tar)")

    #--- Die Installation braucht in ihrer Hilfsumgebung 'cryptography'. Ohne Internet
    #--- kommt es aus wheels/: Linux x86_64 und Windows, Python 3.10 bis 3.14.
    wheels = out_dir / "wheels"
    wheel_targets = {
        "Linux x86_64": ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"],
        "Windows": ["win_amd64"],
    }
    print(f"  Python-Pakete fuer die Installation laden ({SETUP_PIP_REQUIREMENT})...")
    missing_wheels = []
    for label, plats in wheel_targets.items():
        for py in ("3.10", "3.11", "3.12", "3.13", "3.14"):
            plat_args = [x for p in plats for x in ("--platform", p)]
            rc = subprocess.run([sys.executable, "-m", "pip", "download", "--quiet",
                                 "--disable-pip-version-check", "--only-binary=:all:",
                                 *plat_args, "--python-version", py, "--implementation", "cp",
                                 "-d", str(wheels), SETUP_PIP_REQUIREMENT],
                                capture_output=True, check=False).returncode
            if rc != 0:
                missing_wheels.append(f"{label} Python {py}")
    if missing_wheels:
        print(f"  Hinweis: keine Pakete fuer {', '.join(missing_wheels)}; dort braucht die Installation Internet.")

    models: list[dict] = []
    if with_models:
        init_mod = _load_init_stack_module(repo_root)
        env_path = repo_root / ".env"
        env_data = init_mod.parse_env(env_path.read_bytes().decode("utf-8")) if env_path.exists() else {}
        models_dir = Path(_effective_dir(repo_root, env_data, "ARGUS_MODELS_DIR")[0]
                          or os.path.expanduser("~/argus/models"))
        for item in load_models_spec(repo_root / "setup" / "models.json"):
            src = models_dir / item["target"]
            if not src.is_file() or src.stat().st_size != item["size"]:
                raise RuntimeError(f"Modell {item['target']} fehlt in {models_dir} oder ist unvollstaendig.")
            dst = out_dir / "models" / item["target"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"  Kopiere {item['target']}...")
            shutil.copy2(src, dst)
            models.append({"target": item["target"], "size": item["size"], "sha256": item["sha256"]})

    print("  Pruefsumme der Images berechnen...")
    manifest = {
        "format": 1,
        "version": version,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": git_commit,
        "images": targets,
        "images_sha256": _sha256_file(images_tar),
        "models": models,
    }
    (out_dir / BUNDLE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_bundle_manifest(bundle_dir: Path) -> dict:
    path = bundle_dir / BUNDLE_MANIFEST
    if not path.is_file():
        raise ValueError(f"{path} fehlt -- das ist kein Argus-Offline-Paket.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not _VERSION_RE.fullmatch(str(manifest.get("version", ""))):
        raise ValueError("Unbekanntes Format des Offline-Pakets.")
    return manifest


def apply_bundle(repo_root: Path, bundle_dir: Path) -> dict:
    """Prueft die Images eines Offline-Pakets, laedt sie und stellt die .env darauf um:
    Compose nimmt dann nur noch diese Images und baut oder zieht nichts."""
    manifest = load_bundle_manifest(bundle_dir)
    images_tar = bundle_dir / BUNDLE_IMAGES
    print("  Pruefe die Pruefsumme der Images (dauert bei 20 GB eine Weile)...")
    if not images_tar.is_file() or _sha256_file(images_tar) != manifest.get("images_sha256"):
        raise ValueError(f"{images_tar} fehlt oder ist beschaedigt (Pruefsumme stimmt nicht).")
    _run_checked(["docker", "load", "-i", str(images_tar)], repo_root, None, "Images laden")
    missing = [ref for ref in sorted(set(manifest["images"].values())) if not _image_present(ref)]
    if missing:
        raise RuntimeError(f"Nach dem Laden fehlen Images: {', '.join(missing)}")
    shutil.copy2(bundle_dir / BUNDLE_OVERRIDE, repo_root / BUNDLE_OVERRIDE)
    init_mod = _load_init_stack_module(repo_root)
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    init_mod.write_env_file(repo_root / ".env", template_text, {
        #--- Doppelpunkt als Trenner auf jedem System (unter Windows sonst ';').
        "COMPOSE_PATH_SEPARATOR": ":",
        "COMPOSE_FILE": f"docker-compose.yml:{BUNDLE_OVERRIDE}",
        "ARGUS_VERSION": manifest["version"],
        "ARGUS_PULL_POLICY": "never",
    })
    print(f"  Offline-Paket {manifest['version']} geladen ({len(manifest['images'])} Images).")
    return manifest


def copy_models_from_bundle(bundle_dir: Path, models_dir: Path, models_spec_path: Path) -> int:
    """Kopiert Modelle aus dem Paket, die im Modellordner fehlen. Geprueft werden sie
    danach wie heruntergeladene (SHA-256 im Modell-Schritt)."""
    src_root = bundle_dir / "models"
    if not src_root.is_dir():
        return 0
    copied = 0
    for item in load_models_spec(models_spec_path):
        dst = models_dir / item["target"]
        src = src_root / item["target"]
        if (dst.is_file() and dst.stat().st_size == item["size"]) or not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"  Kopiere {item['target']} aus dem Paket...")
        shutil.copy2(src, dst)
        copied += 1
    return copied


def configure_registry_images(repo_root: Path, prefix: str, version: str) -> None:
    """Fertige Images aus einer eigenen Registry (etwa ghcr.io/firma/argus) statt Bau."""
    if not _IMAGE_PREFIX_RE.fullmatch(prefix or ""):
        raise ValueError("Ungueltiger Image-Praefix (klein geschrieben, etwa ghcr.io/firma/argus).")
    if not _VERSION_RE.fullmatch(version or ""):
        raise ValueError("Ungueltige Image-Version.")
    init_mod = _load_init_stack_module(repo_root)
    template_path = repo_root / ".env.example"
    template_text = template_path.read_bytes().decode("utf-8") if template_path.exists() else ""
    init_mod.write_env_file(repo_root / ".env", template_text, {
        "ARGUS_IMAGE_PREFIX": prefix,
        "ARGUS_VERSION": version,
        "ARGUS_PULL_POLICY": "missing",
    })


def run_bundle(repo_root: Path, out: str | None, version: str | None, with_models: bool,
               services: str | None) -> int:
    ver = version or default_bundle_version(repo_root)
    out_dir = Path(out).resolve() if out else Path.cwd() / f"argus-bundle-{ver}"
    svc = [s.strip() for s in services.split(",") if s.strip()] if services else None
    print(f"Erstelle Offline-Paket {ver} in {out_dir}")
    try:
        manifest = create_bundle(repo_root, out_dir, ver, with_models=with_models, services=svc)
    except (RuntimeError, ValueError) as exc:
        print(f"Fehler: {exc}")
        return 1
    size_gb = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file()) / 1024**3
    print(f"\nFertig: {len(manifest['images'])} Images, {len(manifest['models'])} Modelle, {size_gb:.1f} GB.")
    print("Auf dem Zielrechner: argus-src.tar entpacken, dann dort")
    print(f"  ./install.sh --bundle {out_dir}")
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
