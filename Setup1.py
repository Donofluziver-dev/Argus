#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setup1.py -- Infrastruktur, Secrets & Dateigeruest
Zustaendigkeit: Postgres, JWT, Fernet, WebUI-Secret, URLs, Modellnamen,
               Docker Compose, Dockerfiles, requirements.txt, SearxNG,
               generierte Python-Dateien (models, database, main, agent, ingest, tests).
LLM/RAG-Parameter gehoeren NICHT hierher -- das ist Aufgabe von Setup2.py.
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import os
import secrets
import sys
import stat
import textwrap
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
import jwt

# ---------------------------------------------------------------------------
# Bootstrap: setup_common.py wird hier generiert wenn fehlend oder veraltet.
# Dadurch ist die Minimal-Distribution (Setup1-4, Setup_Dashboard.py +
# setup_ssh.ps1) selbsttragend -- bei einer Neuinstallation reichen diese
# Quelldateien; alles andere wird generiert.
# ---------------------------------------------------------------------------
SETUP_COMMON_PY = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_common.py -- Gemeinsame Helper fuer Setup1-4.
Konsolidiert _log, writefile, write_env_block, set_permissions.
Diese Datei wird von Setup1.py beim ersten Lauf automatisch generiert.

ACHTUNG: Quelle der Wahrheit ist der eingebettete SETUP_COMMON_PY-String in
Setup1.py. Direkte Aenderungen an DIESER Datei werden beim naechsten
Setup1-Lauf kommentarlos ueberschrieben -- immer den String in Setup1 pflegen.
"""

import logging
import os
import re
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

#--- Logging

def init_logging() -> logging.Logger:
    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    return logging.getLogger("setup")


log = init_logging()


def _log(msg: str) -> None:
    log.info(msg)


#--- File-Permissions

def current_principal() -> str:
    """Vollqualifizierter Windows-Principal (DOMAIN\\User) des laufenden Prozesses.

    EINE Quelle fuer Python UND setup_ssh.ps1: die PS-Seite setzt ihre ACLs ueber
    WindowsIdentity::GetCurrent().Name (immer DOMAIN\\User), die Python-Seite nutzte
    nur den nackten USERNAME. Laufen beide unter verschiedenen Konten (elevated PS
    als anderer Admin), sperrte jede Seite die andere aus API_Tokens/ aus."""
    import getpass
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    return f"{domain}\\{user}" if domain else user


def _set_windows_owner_only(path: Path) -> None:
    """Owner-only-ACL via icacls. chmod ist unter Windows praktisch ein No-Op
    (setzt nur das Read-only-Bit, keine ACL) -- die Secret-Dateien in API_Tokens/
    waeren sonst je nach Default-ACL auch fuer die lokale 'Users'-Gruppe lesbar."""
    import subprocess
    user = current_principal()
    grant = f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F"
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r"],
                       check=True, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", grant],
                       check=True, capture_output=True)
    except Exception as e:
        _log(f"WARNUNG: icacls-Haertung fuer {path} fehlgeschlagen: {e}")


def set_permissions(path: Path, mode: int) -> None:
    if os.name == "nt":
        _set_windows_owner_only(path)
        return
    try:
        path.chmod(mode)
    except Exception as e:
        _log(f"WARNUNG: chmod fuer {path} nicht moeglich: {e}")


#--- Atomarer Datei-Write (gemeinsamer Kern fuer writefile & write_env_block)

def _atomic_write(path, text: str) -> None:
    """Atomar schreiben: temp-Datei im selben Verzeichnis + os.replace. Bei Abbruch
    bleibt die bestehende Datei intakt (kein halb geschriebenes File)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#--- File-Write mit Skip-on-Equal

def writefile(path, content: str, label: str = "file", do_dedent: bool = True) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    dedented = textwrap.dedent(content).strip() if do_dedent else content.strip()
    normalized_new = "\n".join(
        line.replace("\xa0", " ") for line in dedented.splitlines()
    ) + "\n"

    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8")
            dedented_existing = textwrap.dedent(existing).strip() if do_dedent else existing.strip()
            normalized_existing = "\n".join(
                line.replace("\xa0", " ") for line in dedented_existing.splitlines()
            ) + "\n"
            if normalized_existing == normalized_new:
                return  # unveraendert -- nichts schreiben (Skip ohne Log-Spam)
        except Exception as e:
            _log(f"WARNUNG: Vergleichsfehler bei {p}: {e} -- Datei wird ueberschrieben.")

    _atomic_write(p, normalized_new)
    _log(f"WRITE {label}: {p}")


#--- .env-Block-Manipulation

def write_env_block(
    block_name: str,
    env_vars: dict,
    env_path: Path,
    backup: bool = False,
    dedupe_outside: bool = False,
) -> None:
    """Schreibt benannten Block in .env (idempotent).

    backup=True: erstellt .env.bak.<timestamp> vor dem Schreiben.
    Diese Backups enthalten Secrets und muessen in .gitignore ausgeschlossen sein.
    dedupe_outside=True: entfernt Keys aus diesem Block die ausserhalb stehen.
    Werte werden zwingend in str() konvertiert (Type-Hardening fuer int/bool).
    """
    env_path = Path(env_path)

    if backup and env_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = env_path.with_suffix(f".bak.{ts}")
        _atomic_write(backup_path, env_path.read_text(encoding="utf-8"))

    start_marker = f"# ===== {block_name} BLOCK START ====="
    end_marker = f"# ===== {block_name} BLOCK END ====="

    block_content = start_marker + "\n"
    for k, v in env_vars.items():
        v_str = str(v) if v is not None else ""
        if any(c in v_str for c in ("#", " ", '"', "'", "\\", "$", "`")):
            escaped = v_str.replace("\\", "\\\\").replace('"', '\\"')
            block_content += f'{k}="{escaped}"\n'
        else:
            block_content += f"{k}={v_str}\n"
    block_content += end_marker

    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8")
        pattern = re.compile(
            re.escape(start_marker) + r".*?" + re.escape(end_marker),
            re.DOTALL,
        )
        if pattern.search(existing):
            new_content = pattern.sub(block_content, existing)
        else:
            new_content = existing.rstrip("\n") + "\n\n" + block_content + "\n"
    else:
        new_content = block_content + "\n"

    _atomic_write(env_path, new_content)
    _log(f"ENV-Block '{block_name}' geschrieben in {env_path}")

    if dedupe_outside:
        if prune_env_keys(set(env_vars.keys()), env_path):
            _log(f"Duplikate ausserhalb '{block_name}'-Block entfernt.")


def prune_env_keys(keys, env_path) -> bool:
    """Entfernt die genannten Keys aus der .env, sofern sie AUSSERHALB eines
    benannten BLOCK-Bereichs stehen. Liefert True, wenn etwas geaendert wurde.

    Eine Quelle fuer zwei Aufrufer: write_env_block(dedupe_outside=True) raeumt
    Duplikate des eigenen Blocks weg, Setup3 raeumt Altlasten (WinRM/SSH) weg.
    Setup3 hatte diese Schleife vorher Zeile fuer Zeile nachgebaut."""
    env_path = Path(env_path)
    if not env_path.exists():
        return False
    keys = set(keys)
    content = env_path.read_text(encoding="utf-8")
    in_block = False
    kept = []
    for line in content.splitlines():
        if "BLOCK START" in line:
            in_block = True
        elif "BLOCK END" in line:
            in_block = False
        elif not in_block:
            if "=" in line and not line.lstrip().startswith("#"):
                if line.split("=", 1)[0].strip() in keys:
                    continue
        kept.append(line)
    cleaned = "\n".join(kept)
    if cleaned == content:
        return False
    _atomic_write(env_path, cleaned)
    return True


#--- Cross-File-Marker: Literale, die ueber Dateigrenzen konsistent bleiben muessen.
#--- Eine Prompt-Umformulierung darf ein Marker-Paar nicht still zerreissen.

MARKERS = {
    "Additional results":  ["rag_backend/agent.py"],
    'snippet="true"':      ["rag_backend/agent.py"],
    "<document index":     ["rag_backend/agent.py", "rag_backend/tests/test_main.py"],
    "abgeschlossen":       ["rag_backend/agent.py", "rag_backend/cloud_agents/missions.py"],
    "RESEARCH MATERIAL":   ["rag_backend/cloud_agents/missions.py", "rag_backend/tests/test_cloud_agents.py"],
    "mission planner":     ["rag_backend/cloud_agents/missions.py", "rag_backend/tests/test_cloud_agents.py"],
    "work agent":          ["rag_backend/cloud_agents/missions.py", "rag_backend/tests/test_cloud_agents.py"],
    "quality reviewer":    ["rag_backend/cloud_agents/missions.py", "rag_backend/tests/test_cloud_agents.py"],
    #--- Sicherheitskritische Doppel-Implementierungen: die Pfad-Whitelist lebt
    #--- in zwei Prozessen und MUSS identisch bleiben -- faellt eine Seite weg,
    #--- oeffnet sich ein Traversal-Pfad in den Secrets-Mount.
    "relative_to":         ["rag_backend/agent.py", "rag_backend/mcp_servers/filesystem.py"],
    "resolve(strict=False)": ["rag_backend/agent.py", "rag_backend/mcp_servers/filesystem.py"],
    #--- Der Klassifizierer entschaerft PowerShell-Escapes VOR dem String-Strip.
    "_ESCAPE_PLACEHOLDER": ["rag_backend/action_engine/tools/run_powershell_tool.py"],
}


def verify_markers(base_dir) -> None:
    """Prueft, ob jeder Cross-File-Marker in allen erwarteten, bereits generierten
    Dateien vorkommt. Fehlende Dateien werden uebersprungen (Teil-Laeufe loesen keinen
    Fehlalarm aus). Nur eine Warnung, nie fatal."""
    base = Path(base_dir)
    missing = []
    for marker, rel_paths in MARKERS.items():
        for rel in rel_paths:
            p = base / rel
            if not p.exists():
                continue
            try:
                if marker not in p.read_text(encoding="utf-8"):
                    missing.append(f"{marker!r} fehlt in {rel}")
            except Exception as e:
                _log(f"WARNUNG: Marker-Check {rel} nicht lesbar: {e}")
    if missing:
        _log("WARNUNG: Cross-File-Marker inkonsistent -- ein Marker-Paar wurde evtl. zerrissen:")
        for m in missing:
            _log("   " + m)
    else:
        _log("Cross-File-Marker konsistent.")
'''

_bootstrap_path = Path(__file__).parent.resolve() / "setup_common.py"
_bootstrap_existing = _bootstrap_path.read_text(encoding="utf-8") if _bootstrap_path.exists() else ""
if _bootstrap_existing.replace('\r\n', '\n') != SETUP_COMMON_PY.replace('\r\n', '\n'):
    _bootstrap_path.write_text(SETUP_COMMON_PY, encoding="utf-8")
    print(f"[bootstrap] setup_common.py geschrieben: {_bootstrap_path}")

from setup_common import _log, writefile, write_env_block, set_permissions, verify_markers

BASE_DIR = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Secret-Verwaltung mit Fernet-Verschluesselung
# ---------------------------------------------------------------------------
def _set_fernet_key_permissions(path: Path):
    """Auf Windows owner-only (icacls) -- Docker Desktop liest den Mount mit den
    Rechten des Users, owner-only genuegt also. Nur auf POSIX bleibt 0644 noetig,
    weil dort der Container-User (UID 1000) bei UID-Mismatch sonst nicht lesen kann."""
    if os.name == "nt":
        set_permissions(path, 0)  # -> icacls owner-only
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except Exception as e:
        _log(f"WARNUNG: chmod fuer {path} nicht moeglich: {e}")


def _get_or_create_fernet_key(api_dir: Path) -> Fernet:
    key_path = api_dir / "fernet_key.txt"
    if key_path.exists():
        raw = key_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                fernet_obj = Fernet(raw.encode())
                _set_fernet_key_permissions(key_path)
                return fernet_obj
            except Exception:
                # Bestehender, aber UNGUELTIGER Key: nicht still einen neuen erzeugen
                # -- das macht alle ENC:-Secrets unentschluesselbar und rotiert
                # JWT_SECRET. Sichern und hart abbrechen.
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup_path = key_path.with_name(f"fernet_key.txt.bak.{ts}")
                backup_path.write_text(raw, encoding="utf-8")
                set_permissions(backup_path, stat.S_IRUSR | stat.S_IWUSR)
                _log("ABBRUCH: Bestehender FERNET_KEY ist ungueltig (beschaedigt/falsches Format).")
                _log(f"   Alter Key gesichert nach {backup_path}.")
                _log("   -> Korrekten Key wiederherstellen und zurueckkopieren, ODER")
                _log("      API_Tokens/fernet_key.txt loeschen, um bewusst neu aufzusetzen")
                _log("      (verwirft alle bestehenden ENC:-Secrets, rotiert JWT/WebUI-Secret).")
                sys.exit(1)
    new_key = Fernet.generate_key()
    key_path.write_text(new_key.decode(), encoding="utf-8")
    _set_fernet_key_permissions(key_path)
    _log("Neuer FERNET_KEY generiert und in API_Tokens/fernet_key.txt gespeichert.")
    return Fernet(new_key)


def secret(filename: str, generator, api_dir: Path, fernet: Fernet) -> str:
    file_path = api_dir / filename
    value = None
    was_encrypted = False

    if file_path.exists():
        try:
            raw = file_path.read_text(encoding="utf-8").strip()
            if raw and "<BITTE" not in raw:
                if raw.startswith("ENC:"):
                    was_encrypted = True
                    try:
                        value = fernet.decrypt(raw[4:].encode()).decode()
                    except Exception as e:
                        raise RuntimeError(
                            f"Entschluesselung von {filename} fehlgeschlagen ({e}). "
                            "Datei bleibt unveraendert; korrekten Fernet-Key wiederherstellen oder Secret bewusst rotieren."
                        ) from e
                else:
                    value = raw
        except RuntimeError:
            raise
        except Exception as e:
            _log(f"WARNUNG: Fehler beim Lesen von {filename}: {e}")

    if value is None:
        value = generator()

    if value and not value.startswith("<BITTE") and not was_encrypted:
        stored = "ENC:" + fernet.encrypt(value.encode()).decode()
        file_path.write_text(stored, encoding="utf-8")
        _log(f"Key in {filename} wurde erfolgreich verschluesselt.")

    if file_path.exists():
        set_permissions(file_path, stat.S_IRUSR | stat.S_IWUSR)

    return value


def stable_password(filename: str, api_dir: Path, nbytes: int = 16) -> str:
    """Liest oder erzeugt ein stabiles Klartext-Passwort. Wird NIE von der Fernet-Rotation beruehrt.
    Postgres-Passwort muss stabil bleiben, weil pg_data sonst nicht mehr zugaenglich ist."""
    file_path = api_dir / filename
    if file_path.exists():
        raw = file_path.read_text(encoding="utf-8").strip()
        if raw.startswith("ENC:"):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = file_path.with_name(f"{filename}.bak.{ts}")
            backup.write_text(raw, encoding="utf-8")
            _log(f"ABBRUCH: {filename} ist verschluesselt, muss aber Klartext sein (Postgres liest die Datei direkt).")
            _log(f"   Gesichert nach {backup} -- Klartext-Passwort wiederherstellen, dann Setup1 erneut ausfuehren.")
            sys.exit(1)
        if raw and "<BITTE" not in raw:
            set_permissions(file_path, stat.S_IRUSR | stat.S_IWUSR)
            return raw
    value = secrets.token_urlsafe(nbytes)
    file_path.write_text(value, encoding="utf-8")
    set_permissions(file_path, stat.S_IRUSR | stat.S_IWUSR)
    _log(f"Stabiles Passwort in {filename} generiert (nicht Teil der Fernet-Rotation).")
    return value


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------
if __name__ == "__main__":

    # Setup1 schreibt mit RELATIVEN Pfaden, die .env laeuft ueber BASE_DIR.
    # Ohne chdir landet beides in getrennten Baeumen.
    os.chdir(BASE_DIR)

    # ---------------------------------------------------------------------------
    # API_Tokens & Secrets
    # ---------------------------------------------------------------------------
    API_DIR = Path("API_Tokens")
    API_DIR.mkdir(parents=True, exist_ok=True)
    set_permissions(API_DIR, stat.S_IRWXU)

    _fernet = _get_or_create_fernet_key(API_DIR)

    #--- Credential-Files als leere Platzhalter anlegen. Fehlt eine Datei beim
    #--- 'up', legt Docker Desktop unter Windows an ihrer Stelle ein VERZEICHNIS
    #--- an, und der Mount ist danach nicht mehr zu reparieren.
    #--- Gemini.txt gehoert fachlich zu Setup4, wird aber hier angelegt, weil
    #--- docker-compose.yml sie mountet. Leer = Cloud-Agenten deaktiviert.
    #--- ssh_key ist der private Ed25519-Key aus setup_ssh.ps1.
    for _ssh_file in ("ssh_user.txt", "ssh_key", "ssh_password.txt", "telegram.txt",
                      "Gemini.txt"):
        _p = API_DIR / _ssh_file
        if not _p.exists():
            _p.write_text("", encoding="utf-8")
            set_permissions(_p, stat.S_IRUSR | stat.S_IWUSR)

    #--- Workspace-Skelett defensiv anlegen (sonst degeneriert der Bind-Mount).
    #--- Setup2 befuellt identity/ und users/_defaults/.
    for _sub in ("identity", "users/_defaults", "logs", "Evals"):
        (Path("C:/Argus_Workspace") / _sub).mkdir(parents=True, exist_ok=True)

    #--- voice_ref/ defensiv anlegen. Enthaelt die geklonte Referenzstimme --
    #--- Userdaten, nicht generierbar, daher mit den Setup-Dateien sichern.
    #--- Fehlt der Ordner, faellt der TTS-Wrapper auf den Preset-Sprecher zurueck.
    (BASE_DIR / "voice_ref").mkdir(exist_ok=True)

    def _s(filename, generator):
        return secret(filename, generator, API_DIR, _fernet)

    #--- Migration: vllm_api_key.txt -> sglang_api_key.txt. Idempotent.
    _old_api_key = API_DIR / "vllm_api_key.txt"
    _new_api_key = API_DIR / "sglang_api_key.txt"
    if _old_api_key.exists() and not _new_api_key.exists():
        _old_api_key.rename(_new_api_key)
        _log("Migration: API_Tokens/vllm_api_key.txt -> sglang_api_key.txt (Altname vLLM->SGLang).")

    env: dict[str, str] = {
        "POSTGRES_USER":     "argus_user",
        "POSTGRES_PASSWORD": stable_password("postgres_password.txt", API_DIR),
        "POSTGRES_DB":       "ragdb",

        "HF_TOKEN":          _s("HF_TOKEN.txt",          lambda: "<BITTE_HF_TOKEN_EINFUEGEN>"),
        "LANGCHAIN_API_KEY": _s("LANGSMITH_API_KEY.txt",  lambda: "<BITTE_LANGSMITH_KEY_EINFUEGEN>"),

        "JWT_SECRET":        _s("jwt_secret.txt",         lambda: secrets.token_urlsafe(64)),
        "WEBUI_SECRET_KEY":  _s("webui_secret_key.txt",   lambda: secrets.token_urlsafe(48)),

        "QDRANT_URL":        "http://qdrant:6333",
        "QDRANT_COLLECTION": "docs",
        "TTS_SERVICE_URL":   "http://tts-service:8002",
        "STT_SERVICE_URL":   "http://stt-service:8003",

        # Qwen3-TTS Stimm-/Sprach-Knobs, per .env ohne Rebuild umschaltbar.
        # QWEN_VOICE leer = Default-Sprecher; waehlbar: vivian, serena, ryan,
        # aiden, eric, dylan, uncle_fu, ono_anna, sohee.
        # Geklonte Stimme (Default an): Referenz liegt in voice_ref/, read-only
        # gemountet. Fehlt sie, faellt der Wrapper auf den Preset zurueck.
        "QWEN_LANG":            "auto",
        "QWEN_VOICE":           "",
        "QWEN_CLONE_REF_WAV":   "",
        "QWEN_CLONE_REF_TEXT":  "/app/voice_ref/stimme.txt",
        "QWEN_CLONE_REF_SPK":   "/app/voice_ref/stimme.spk",
        "QWEN_CLONE_REF_RVQ":   "/app/voice_ref/stimme.rvq",
        # Fester Sampling-Seed ueber ALLE Saetze -- der Upstream-Default -1 zieht
        # pro Satz einen neuen Seed und erzeugt hoerbares Stimm-Drift.
        # QWEN_TEMP leer = Modell-Default 0.9; 0.7 ist stabiler fuer Clone-Stimmen.
        "QWEN_SEED":            "8119056094318024353",
        "QWEN_TEMP":            "0.7",
        # Base-Talker (Clone-Modell). Nur das 1.7B-Q8 ist ins Image gebacken.
        "QWEN_BASE_TALKER":     "/app/models/qwen-talker-1.7b-base-Q8_0.gguf",
        #    Audio-Cache des tts-service: gleicher Text = gleiche Bytes, ein
        #    zweites Vorlesen kostet dann nichts. 0 Eintraege schaltet ihn aus.
        "TTS_CACHE_MAX_ENTRIES": "64",
        "TTS_CACHE_MAX_MB":      "256",
        "OMP_NUM_THREADS":      "1",
        "GGML_NUM_THREADS":     "6",
        "OPENBLAS_NUM_THREADS": "1",

        "RERANKER_MODEL":    "BAAI/bge-reranker-v2-m3",
        "EMBEDDING_MODEL":   "BAAI/bge-m3",

        "LANGCHAIN_TRACING_V2": "false",
        "LANGCHAIN_ENDPOINT":   "https://eu.api.smith.langchain.com",
        "LANGCHAIN_PROJECT":    "Argus",

        #--- Lokales Tracing (Phoenix). Leerer Endpoint = Tracing aus.
        "PHOENIX_COLLECTOR_ENDPOINT": "http://phoenix:6006",
        "PHOENIX_PROJECT_NAME":       "argus",

        "SEARXNG_SECRET":        _s("searxng_secret_key.txt", lambda: secrets.token_urlsafe(32)),
        "SGLANG_API_KEY":          _s("sglang_api_key.txt",       lambda: secrets.token_urlsafe(32)),
        "AUDIT_HMAC_KEY":        _s("audit_hmac_key.txt",     lambda: secrets.token_urlsafe(48)),

        "CORS_ORIGINS":          "http://localhost:3000",
        "ENABLE_BACKEND_SIGNUP": "false",

        #--- Sicherheits-/Betriebsschalter des Backends. Die Werte SIND die
        #--- os.getenv-Defaults im generierten Code und stehen hier, damit man sie
        #--- in der .env sieht. Aendern: HIER, nicht in der .env.
        #    Lebensdauer der Login-Tokens und Rotationsfenster der WebUI-Session:
        "JWT_EXPIRE_DAYS":        "1",
        "SESSION_ROTATE_HOURS":   "6",
        #    Rate-Limit des Chat-Endpoints (slowapi-Syntax):
        "CHAT_RATE_LIMIT":        "60/minute",
        #    Dasselbe fuer den Argus-Chat -- er kostet GPU-Zeit wie jeder Turn:
        "DASHBOARD_CHAT_RATE_LIMIT": "60/minute",
        #    Sprachwege (Mikro/Sprachausgabe) und Datei-Uploads:
        "DASHBOARD_VOICE_RATE_LIMIT":  "60/minute",
        "DASHBOARD_UPLOAD_RATE_LIMIT": "30/minute",
        #    Anmeldeversuche am Dashboard. Raten sind die einzige Bremse gegen
        #    Durchprobieren:
        "DASHBOARD_LOGIN_RATE_LIMIT":  "10/minute",
        #    Selbstregistrierung: neue Konten bekommen IMMER die Rolle 'chat'.
        #    "false" = nur Administratoren legen Konten an:
        "DASHBOARD_ALLOW_REGISTER":     "true",
        "DASHBOARD_REGISTER_RATE_LIMIT": "5/minute",
        #    Laufzeit einer Dashboard-Anmeldung in Stunden (danach neu anmelden):
        "DASHBOARD_SESSION_HOURS": "12",
        #    Obergrenze je hochgeladener Datei (Bytes):
        "UPLOAD_MAX_BYTES":       str(500 * 1024 * 1024),
        #    Wie lange in den Chat gezogene Dateien im Uploads-Ordner liegen
        #    bleiben, bevor sie automatisch verschwinden. 0 = nie aufraeumen.
        #    Dauerhaft Auffindbares gehoert in den Index, nicht in diesen Ordner:
        "UPLOAD_RETENTION_HOURS": "24",
        "UPLOAD_SWEEP_SECONDS":   "3600",
        #    Host-Ordner fuer Eval-Laeufe (Dashboard-Panel) und Action-Audit-Dateien,
        #    gemountet als /app/evals:
        "ARGUS_EVALS_DIR":        "C:/Argus_Workspace/Evals",
        #    Origins, die schreibende Dashboard-Endpoints aufrufen duerfen (CSRF-Gate):
        "DASHBOARD_ORIGINS":      "http://127.0.0.1:7860,http://localhost:7860",
        #    Namen, unter denen das Backend ansprechbar ist (Host-Header-Gate).
        #    'rag-backend' braucht open-webui, 'localhost' der Healthcheck:
        "TRUSTED_HOSTS":          "127.0.0.1,localhost,rag-backend",
        #    Notschalter: ueberspringt die Alembic-Migrationen beim Start:
        "SKIP_MIGRATIONS":        "false",
        #    Reflexions-Loop: Leerlauf bis zum Start, Prueftakt (Sekunden):
        "REFLECTION_IDLE_SECONDS":   "300",
        "REFLECTION_CHECK_INTERVAL": "120",

        # LLM/RAG-Parameter schreibt Setup2 in den SETUP2-Block.
        # OPENAI_API_KEY wird unten auf den JWT-Service-Token gesetzt.
        "OPENAI_API_KEY":              "",
    }

    _log("Pruefe JWT-Servicetoken fuer Open WebUI...")
    _JWT_VALIDITY_DAYS = 90
    _JWT_RENEW_THRESHOLD_DAYS = 30
    _existing_token_path = API_DIR / "service_token.txt"
    _needs_new_token = True

    if _existing_token_path.exists():
        try:
            _raw_token = _existing_token_path.read_text(encoding="utf-8").strip()
            _decoded = jwt.decode(_raw_token, env["JWT_SECRET"], algorithms=["HS256"])
            _exp_ts = _decoded.get("exp", 0)
            _exp_dt = datetime.fromtimestamp(_exp_ts, tz=timezone.utc)
            _remaining = (_exp_dt - datetime.now(timezone.utc)).days
            if _remaining > _JWT_RENEW_THRESHOLD_DAYS:
                env["OPENAI_API_KEY"] = _raw_token
                _needs_new_token = False
                _log(f"Service-Token gueltig bis: {_exp_dt.strftime('%Y-%m-%d')} (noch {_remaining} Tage) -- kein Erneuerungsbedarf.")
            else:
                _log(f"HINWEIS: Service-Token laeuft in {_remaining} Tagen ab ({_exp_dt.strftime('%Y-%m-%d')}) -- wird erneuert.")
        except Exception as e:
            _log(f"WARNUNG: Bestehender Service-Token ungueltig ({e}) -- wird neu generiert.")

    if _needs_new_token:
        _exp_new = datetime.now(timezone.utc) + timedelta(days=_JWT_VALIDITY_DAYS)
        _service_payload = {"sub": "service", "exp": _exp_new}
        _new_token = jwt.encode(_service_payload, env["JWT_SECRET"], algorithm="HS256")
        env["OPENAI_API_KEY"] = _new_token
        _existing_token_path.write_text(_new_token, encoding="utf-8")
        set_permissions(_existing_token_path, stat.S_IRUSR | stat.S_IWUSR)
        _log(f"Neuer Service-Token generiert, gueltig bis: {_exp_new.strftime('%Y-%m-%d')} (+{_JWT_VALIDITY_DAYS} Tage).")
    _log("Service-Token als OPENAI_API_KEY gesetzt.")

    env["DATABASE_URL"] = (
        f"postgresql://{env['POSTGRES_USER']}"
        f"@postgres:5432/{env['POSTGRES_DB']}"
    )

    _log("\n--- Ueberpruefung der API-Token-Dateien ---")
    _PLACEHOLDER_CHECK = {
        "HF_TOKEN":          "HF_TOKEN.txt",
        "LANGCHAIN_API_KEY": "LANGSMITH_API_KEY.txt",
    }
    missing_tokens = [fname for env_key, fname in _PLACEHOLDER_CHECK.items()
                      if env.get(env_key, "").startswith("<BITTE")]

    if missing_tokens:
        _log("WICHTIG: Bitte trage deine echten API-Schluessel in folgende Dateien ein:")
        for t in missing_tokens:
            _log(f"   - API_Tokens/{t}")
        # Platzhalter fuer ALLE fehlenden Keys anlegen, nicht nur den ersten.
        for env_key, fname in _PLACEHOLDER_CHECK.items():
            if env[env_key].startswith("<BITTE"):
                fpath = API_DIR / fname
                if not fpath.exists():
                    fpath.write_text(env[env_key], encoding="utf-8")
                    set_permissions(fpath, stat.S_IRUSR | stat.S_IWUSR)
                    _log(f"Platzhalter-Datei angelegt: {fpath} -- bitte befuellen.")
        if "HF_TOKEN.txt" in missing_tokens:
            _log("ABBRUCH: HF_TOKEN ist Pflicht (SGLang kann ohne kein Modell laden).")
            _log("Nach Eintrag der Keys setup1.py erneut ausfuehren -- Keys werden dann verschluesselt.")
            _log(".env wurde NICHT geschrieben -- Container wuerden sonst mit Platzhaltern starten.")
            sys.exit(1)
        _log("HINWEIS: Die fehlenden Keys sind optional -- Betrieb laeuft ohne sie weiter.")
    else:
        _log("Alle API-Token-Dateien sind korrekt befuellt.")
    #--- Tracing-Ziel: lokal (Phoenix) hat Vorrang vor der LangSmith-Cloud.
    #--- Wer LangSmith bewusst will: PHOENIX_COLLECTOR_ENDPOINT leeren.
    if env.get("PHOENIX_COLLECTOR_ENDPOINT"):
        env["LANGCHAIN_TRACING_V2"] = "false"
        _log("Tracing: Phoenix lokal (LangSmith-Cloud bleibt aus).")
    elif "LANGCHAIN_API_KEY" in env and not env["LANGCHAIN_API_KEY"].startswith("<BITTE"):
        env["LANGCHAIN_TRACING_V2"] = "true"
        _log("LangSmith Tracing aktiviert (kein lokaler Phoenix-Endpoint gesetzt).")
    else:
        env["LANGCHAIN_TRACING_V2"] = "false"
        _log("Tracing deaktiviert (weder Phoenix-Endpoint noch LangSmith-Key).")

    # Sensitive Keys liegen verschluesselt in API_Tokens/ und kommen per
    # Volume-Mount in die Container.
    sensitive_keys = {
        "POSTGRES_PASSWORD", "HF_TOKEN", "LANGCHAIN_API_KEY",
        "JWT_SECRET", "WEBUI_SECRET_KEY", "SEARXNG_SECRET", "SGLANG_API_KEY",
        "AUDIT_HMAC_KEY",
    }
    setup1_only = {k: v for k, v in env.items() if k not in sensitive_keys}
    write_env_block("SETUP1", setup1_only, BASE_DIR / ".env", backup=False)


    # ---------------------------------------------------------------------------
    # pyproject.toml
    # ---------------------------------------------------------------------------
    #--- Versions-Politik: UNTERGRENZE = die verifizierte Version, OBERGRENZE =
    #--- naechstes Major. Aktualisieren: Version hochziehen, rebuilden, testen --
    #--- nicht den Cap entfernen.
    PYPROJECT_TOML = textwrap.dedent("""\
        [project]
        name = "rag-backend"
        version = "0.1.0"
        description = "Argus RAG Backend"
        requires-python = ">=3.12"
        dependencies = [
            "fastapi>=0.139.0,<1.0",
            "uvicorn[standard]>=0.51.0,<1.0",
            "sqlalchemy>=2.0.51,<3.0",
            "alembic>=1.18.5,<2.0",
            "psycopg2-binary>=2.9.12,<3.0",
            "argon2-cffi>=25.1.0,<26.0",
            "PyJWT>=2.13.0,<3.0",
            "cryptography>=50.0.0,<51.0",
            "httpx>=0.28.1,<1.0",
            "python-multipart>=0.0.32,<1.0",
            "python-dotenv>=1.2.2,<2.0",
            "tzdata>=2026.3",
            "slowapi>=0.1.10,<1.0",
            "pytest>=9.1.1,<10.0",
            "pytest-asyncio>=1.4.0,<2.0",
            "qdrant-client>=1.18.0,<2.0",
            "sentence-transformers>=5.6.0,<6.0",
            "scipy>=1.18.0,<2.0",
            "transformers>=5.13.1,<6.0",
            "PyYAML>=6.0.3,<7.0",
            "openai>=3.0.0,<4.0",
            "tiktoken>=0.13.0,<1.0",
            "pypdfium2>=5.11.0,<6.0",
            "pdfplumber>=0.11.10,<1.0",
            "python-docx>=1.2.0,<2.0",
            "python-pptx>=1.0.2,<2.0",
            "openpyxl>=3.1.5,<4.0",
            "langchain>=1.3.13,<2.0",
            "langchain-core>=1.4.9,<2.0",
            "langchain-openai>=1.3.5,<2.0",
            "langchain-text-splitters>=1.1.2,<2.0",
            "langgraph>=1.2.9,<2.0",
            "langsmith>=0.10.5,<1.0",
            # Lokales Tracing nach Phoenix (OTLP). Der LangChain-Instrumentor
            # erfasst alle LangChain-Pakete.
            "arize-phoenix-otel>=0.16.1,<1.0",
            "openinference-instrumentation-langchain>=0.1.67,<1.0",
            "EbookLib>=0.20,<1.0",
            "beautifulsoup4>=4.15.0,<5.0",
            "torch>=2.13.0,<3.0",
            "paramiko>=5.0.0,<6.0",
            "python-telegram-bot>=22.8,<23.0",
            "langchain-mcp-adapters>=0.3.0,<1.0",
            "mcp>=1.28.1,<2.0",
            "google-genai>=2.11.0,<3.0",
        ]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        # Auch hier, nicht nur in pytest.ini: die ini liegt im Projektordner und
        # wird NICHT ins Image kopiert. Ohne asyncio_mode scheitert im Container
        # jeder async-Test.
        [tool.pytest.ini_options]
        asyncio_mode = "auto"
    """)
    writefile("rag_backend/pyproject.toml", PYPROJECT_TOML, "backend pyproject.toml", do_dedent=False)


    # ---------------------------------------------------------------------------
    # SearxNG settings.yml
    # ---------------------------------------------------------------------------
    SEARXNG_SETTINGS = f"""\
use_default_settings: true

server:
  port: 8080
  bind_address: "0.0.0.0"
  secret_key: "{env['SEARXNG_SECRET']}"

search:
  formats:
    - html
    - json

limiter: false

ui:
  static_use_hash: true
"""
    writefile("searxng_config/settings.yml", SEARXNG_SETTINGS, "searxng config", do_dedent=False)
    #--- Owner-only wie alles in API_Tokens/: die Datei traegt den SEARXNG_SECRET
    #--- im Klartext. Der Bind-Mount bleibt lesbar, weil Docker Desktop mit den
    #--- Rechten des angemeldeten Users zugreift.
    set_permissions(Path("searxng_config/settings.yml"), stat.S_IRUSR | stat.S_IWUSR)


    # ---------------------------------------------------------------------------
    # docker-compose.yml
    # ---------------------------------------------------------------------------
    COMPOSE = textwrap.dedent("""\
    networks:
      backend:
      sandbox:
        internal: true

    services:
      sglang:
        build:
          context: .
          dockerfile: Dockerfile.blackwell
        restart: unless-stopped
        networks:
          - backend
        environment:
          - OAI_MODEL=${OAI_MODEL}
          - TORCH_CUDA_ARCH_LIST=12.0
          # AWQ-Checkpoint hat kein ue8m0-Scale-Format -> DeepGemm-JIT-Kernel
          # crasht auf Blackwell sm_120. Abschalten erzwingt den Fallback-GEMM.
          - SGLANG_ENABLE_JIT_DEEPGEMM=0
          - SGLANG_QUANTIZATION=${SGLANG_QUANTIZATION}
          - SGLANG_MAX_SEQUENCE_LEN=${SGLANG_MAX_SEQUENCE_LEN}
          - SGLANG_MEM_FRACTION_STATIC=${SGLANG_MEM_FRACTION_STATIC}
          - SGLANG_CUDA_GRAPH_MAX_BS=${SGLANG_CUDA_GRAPH_MAX_BS}
          - SGLANG_CHUNKED_PREFILL_SIZE=${SGLANG_CHUNKED_PREFILL_SIZE}
          - SGLANG_MAX_RUNNING_REQUESTS=${SGLANG_MAX_RUNNING_REQUESTS}
          # KV-Pool-Pin: speist --max-total-tokens. Quelle ist der Setup2-Knob
          # sglang_kv_pool_tokens; ohne ihn greift nur der 20000-Fallback.
          - SGLANG_MAX_TOTAL_TOKENS=${SGLANG_KV_POOL_TOKENS:-20000}
          - SGLANG_TOOL_CALL_PARSER=${SGLANG_TOOL_CALL_PARSER}
          - SGLANG_REASONING_PARSER=${SGLANG_REASONING_PARSER}
          - SGLANG_CHAT_TEMPLATE_KWARGS=${SGLANG_CHAT_TEMPLATE_KWARGS}
          # Optionales Pinning der Modell-Revision (Supply-Chain/LLM03): leer = aktuelles Verhalten.
          - SGLANG_MODEL_REVISION=${SGLANG_MODEL_REVISION:-}
          # dtype-Override: Qwen3.5-GDN braucht Modell-dtype == fp16-KV/Conv-Cache, sonst bf16/fp16-Mismatch im causal_conv1d-Triton-Kernel.
          - SGLANG_DTYPE=${SGLANG_DTYPE:-}
          - SGLANG_MAMBA_CONV_DTYPE=${SGLANG_MAMBA_CONV_DTYPE:-}
          # true -> --enable-multimodal. Nach dem ersten Multimodal-Boot
          # max_total_num_tokens im Log pruefen (Vision-Encoder kostet VRAM).
          - SGLANG_ENABLE_MULTIMODAL=${SGLANG_ENABLE_MULTIMODAL:-}
        shm_size: 16gb
        command: >
          bash -c '
          if [ -f /run/secrets/hf_token ]; then export HF_TOKEN="$$(python /usr/local/bin/decrypt_secret.py /run/secrets/hf_token)"; fi;
          if [ -f /run/secrets/sglang_api_key ]; then export SGLANG_API_KEY="$$(python /usr/local/bin/decrypt_secret.py /run/secrets/sglang_api_key)"; fi;
          if [ -n "$${SGLANG_QUANTIZATION}" ]; then QUANT_FLAG="--quantization $${SGLANG_QUANTIZATION}"; else QUANT_FLAG=""; fi;
          if [ -n "$${SGLANG_TOOL_CALL_PARSER}" ]; then TCP_FLAG="--tool-call-parser $${SGLANG_TOOL_CALL_PARSER}"; else TCP_FLAG=""; fi;
          if [ -n "$${SGLANG_REASONING_PARSER}" ]; then RP_FLAG="--reasoning-parser $${SGLANG_REASONING_PARSER}"; else RP_FLAG=""; fi;
          if [ -n "$${SGLANG_CHAT_TEMPLATE_KWARGS}" ]; then CTK_ARGS=(--chat-template-kwargs "$${SGLANG_CHAT_TEMPLATE_KWARGS}"); else CTK_ARGS=(); fi;
          if [ -n "$${SGLANG_MODEL_REVISION}" ]; then REV_FLAG="--revision $${SGLANG_MODEL_REVISION}"; else REV_FLAG=""; fi;
          if [ -n "$${SGLANG_DTYPE}" ]; then DTYPE_FLAG="--dtype $${SGLANG_DTYPE}"; else DTYPE_FLAG=""; fi;
          if [ "$${SGLANG_ENABLE_MULTIMODAL}" = "true" ]; then MM_FLAG="--enable-multimodal"; else MM_FLAG=""; fi;
          if [ -n "$${SGLANG_CUDA_GRAPH_MAX_BS}" ]; then CGMB_FLAG="--cuda-graph-max-bs $${SGLANG_CUDA_GRAPH_MAX_BS}"; else CGMB_FLAG=""; fi;
          if [ -n "$${SGLANG_CHUNKED_PREFILL_SIZE}" ]; then CPS_FLAG="--chunked-prefill-size $${SGLANG_CHUNKED_PREFILL_SIZE}"; else CPS_FLAG=""; fi;
          python3 -m sglang.launch_server
          --model-path "$${OAI_MODEL}"
          --trust-remote-code
          --context-length "$${SGLANG_MAX_SEQUENCE_LEN:-20000}"
          --max-total-tokens "$${SGLANG_MAX_TOTAL_TOKENS:-20000}"
          --mem-fraction-static "$${SGLANG_MEM_FRACTION_STATIC:-0.88}"
          --max-running-requests "$${SGLANG_MAX_RUNNING_REQUESTS:-3}"
          --host 0.0.0.0
          --port 30000
          --enable-metrics
          --api-key "$$SGLANG_API_KEY"
          --allow-auto-truncate
          $$QUANT_FLAG $$TCP_FLAG $$RP_FLAG "$${CTK_ARGS[@]}" $$REV_FLAG $$DTYPE_FLAG $$MM_FLAG $$CGMB_FLAG $$CPS_FLAG
          '
        volumes:
          - ./model_cache:/root/.cache/huggingface
          - ./API_Tokens/fernet_key.txt:/run/secrets/fernet_key:ro
          - ./API_Tokens/HF_TOKEN.txt:/run/secrets/hf_token:ro
          - ./API_Tokens/sglang_api_key.txt:/run/secrets/sglang_api_key:ro
        deploy:
          resources:
            reservations:
              devices:
                - driver: nvidia
                  count: 1
                  capabilities: [gpu]
        healthcheck:
          test: ["CMD-SHELL", "curl -sf http://localhost:30000/health || exit 1"]
          interval: 60s
          timeout: 20s
          retries: 5
          start_period: 1200s

      postgres:
        image: postgres:18-alpine@sha256:a1d02e4bd40c94d3bf2bdd3678c137388e76d9efcd23c285e9429d336a834b44
        restart: unless-stopped
        networks:
          - backend
        environment:
          - POSTGRES_USER=${POSTGRES_USER}
          - POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
          - POSTGRES_DB=${POSTGRES_DB}
        volumes:
          - pg_data:/var/lib/postgresql
          - ./API_Tokens/postgres_password.txt:/run/secrets/postgres_password:ro
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
          interval: 40s
          timeout: 5s
          retries: 5

      qdrant:
        build:
          context: .
          dockerfile: Dockerfile.qdrant
        restart: unless-stopped
        networks:
          - backend
        volumes:
          - qdrant_data:/qdrant/storage
        healthcheck:
          test: ["CMD-SHELL", "curl -f http://localhost:6333/healthz || exit 1"]
          interval: 25s
          timeout: 10s
          retries: 5
          start_period: 20s

      searxng:
        image: searxng/searxng:2026.8.14-094c33d40@sha256:892cf809341915a4b7710d3c9045005b4c377d51335a089b6d4da0b28750788d
        networks:
          - backend
        volumes:
          - ./searxng_config:/etc/searxng
        restart: unless-stopped
        healthcheck:
          test: ["CMD-SHELL", "wget -qO- http://localhost:8080/ || exit 1"]
          interval: 30s
          timeout: 10s
          retries: 5
          start_period: 30s

      calc-sandbox:
        build:
          context: ./calc_sandbox
          dockerfile: Dockerfile.sandbox
        restart: unless-stopped
        networks:
          - sandbox
        read_only: true
        tmpfs:
          - /tmp:size=64m,noexec
        volumes:
          # Nur-Lese-Sicht auf den Workspace: execute_code darf lesen, nicht
          # schreiben. Keine Secrets in diesen Container gemountet.
          - C:/Argus_Workspace:/app/data:ro
        security_opt:
          - no-new-privileges:true
        cap_drop:
          - ALL
        mem_limit: 512m
        cpus: "1.0"
        # Prozess-/Thread-Obergrenze gegen Fork-Bomben. 256 laesst dem
        # .NET-Threadpool von pwsh Luft.
        pids_limit: 256
        healthcheck:
          test: ["CMD-SHELL", "wget -qO- http://localhost:8001/healthz || exit 1"]
          interval: 30s
          timeout: 5s
          retries: 3
          start_period: 15s

      tts-service:
        build:
          context: ./tts_service
          dockerfile: Dockerfile.tts
        restart: unless-stopped
        networks:
          - backend
        read_only: true
        tmpfs:
          - /tmp:size=256m
        environment:
          - QWEN_LANG=${QWEN_LANG:-auto}
          - QWEN_VOICE=${QWEN_VOICE:-}
          - QWEN_CLONE_REF_WAV=${QWEN_CLONE_REF_WAV:-}
          - QWEN_CLONE_REF_TEXT=${QWEN_CLONE_REF_TEXT:-}
          - QWEN_CLONE_REF_SPK=${QWEN_CLONE_REF_SPK:-}
          - QWEN_CLONE_REF_RVQ=${QWEN_CLONE_REF_RVQ:-}
          - QWEN_SEED=${QWEN_SEED:-}
          - QWEN_TEMP=${QWEN_TEMP:-0.7}
          - QWEN_BASE_TALKER=${QWEN_BASE_TALKER:-/app/models/qwen-talker-1.7b-base-Q8_0.gguf}
          # Audio-Cache: gleicher Text = gleiche Bytes, zweites Vorlesen ist gratis.
          - TTS_CACHE_MAX_ENTRIES=${TTS_CACHE_MAX_ENTRIES:-64}
          - TTS_CACHE_MAX_MB=${TTS_CACHE_MAX_MB:-256}
          # OMP/OPENBLAS auf 1 gegen Oversubscription neben den GGML-Threads.
          # GGML_NUM_THREADS=6 = gemessener Sweet Spot (SMT im BIOS aus).
          - OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}
          - GGML_NUM_THREADS=${GGML_NUM_THREADS:-6}
          - OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
        volumes:
          # Referenzstimme aus dem Repo (read-only) -> mit den Setup-Dateien versionier-/sicherbar.
          - ./voice_ref:/app/voice_ref:ro
        security_opt:
          - no-new-privileges:true
        cap_drop:
          - ALL
        mem_limit: 12g
        ulimits:
          memlock: -1
        healthcheck:
          test: ["CMD-SHELL", "wget -qO- http://localhost:8002/healthz || exit 1"]
          interval: 30s
          timeout: 5s
          retries: 3
          start_period: 90s

      stt-service:
        build:
          context: ./stt_service
          dockerfile: Dockerfile.stt
        restart: unless-stopped
        networks:
          - backend
        read_only: true
        tmpfs:
          - /tmp:size=128m,noexec
        security_opt:
          - no-new-privileges:true
        cap_drop:
          - ALL
        mem_limit: 3g
        cpus: "4.0"
        volumes:
          - whisper_cache:/home/sttuser/.cache/huggingface
        healthcheck:
          test: ["CMD-SHELL", "wget -qO- http://localhost:8003/healthz || exit 1"]
          interval: 30s
          timeout: 5s
          retries: 3
          start_period: 60s

      rag-backend:
        build:
          context: ./rag_backend
          dockerfile: Dockerfile.api
        env_file: .env
        restart: unless-stopped
        networks:
          - backend
          - sandbox
        extra_hosts:
          - "host.docker.internal:host-gateway"
        ports:
          - "127.0.0.1:7860:7860"
        mem_limit: 8g
        cpus: "6.0"
        volumes:
          - ./documents_to_ingest:/app/documents_to_ingest
          - ./alembic:/app/alembic
          - ./alembic.ini:/app/alembic.ini
          - ./API_Tokens/fernet_key.txt:/app/fernet_key.txt:ro
          - ./API_Tokens/ssh_user.txt:/run/secrets/ssh_user:ro
          - ./API_Tokens/ssh_key:/run/secrets/ssh_key:ro
          - ./API_Tokens/ssh_password.txt:/run/secrets/ssh_password:ro
          - ./API_Tokens/postgres_password.txt:/run/secrets/postgres_password:ro
          - ./API_Tokens/HF_TOKEN.txt:/run/secrets/hf_token:ro
          - ./API_Tokens/jwt_secret.txt:/run/secrets/jwt_secret:ro
          - ./API_Tokens/webui_secret_key.txt:/run/secrets/webui_secret_key:ro
          - ./API_Tokens/sglang_api_key.txt:/run/secrets/sglang_api_key:ro
          - ./API_Tokens/audit_hmac_key.txt:/run/secrets/audit_hmac_key:ro
          - ./API_Tokens/telegram.txt:/run/secrets/telegram:ro
          - ./API_Tokens/LANGSMITH_API_KEY.txt:/run/secrets/langchain_api_key:ro
          - ./API_Tokens/Gemini.txt:/run/secrets/gemini_api_key:ro
          - C:/Argus_Workspace:/host/argus_workspace:rw
          # Eval-Laeufe und Action-Audit-Dateien. Host-Pfad per ARGUS_EVALS_DIR (.env).
          - ${ARGUS_EVALS_DIR:-C:/Argus_Workspace/Evals}:/app/evals:rw
        environment:
          - PYTHONPATH=/app
        # Haertung wie bei tts/stt/calc-sandbox. Kein read_only: uvicorn und
        # alembic schreiben nach /app.
        security_opt:
          - no-new-privileges:true
        cap_drop:
          - ALL
        depends_on:
          postgres:
            condition: service_healthy
          qdrant:
            condition: service_healthy
          searxng:
            condition: service_healthy
          calc-sandbox:
            condition: service_healthy
          sglang:
            condition: service_healthy
        healthcheck:
          test: ["CMD-SHELL", "wget -qO- http://localhost:7860/healthz || exit 1"]
          interval: 60s
          timeout: 10s
          retries: 5
          start_period: 60s

      open-webui:
        # Digest-gepinnt wie alle anderen Images. Aktualisieren:
        #   docker buildx imagetools inspect ghcr.io/open-webui/open-webui:main --format '{{.Manifest.Digest}}'
        image: ghcr.io/open-webui/open-webui:main@sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d
        restart: unless-stopped
        networks:
          - backend
        ports:
          - "127.0.0.1:3000:8080"
        environment:
          - WEBUI_SECRET_KEY_FILE=/run/secrets/webui_secret_key
          - OPENAI_API_BASE_URL=http://rag-backend:7860/v1
          - OPENAI_API_KEY=${OPENAI_API_KEY}
          - DEFAULT_USER_ROLE=user
          - ENABLE_SIGNUP=${OPEN_WEBUI_ENABLE_SIGNUP:-false}
          - ENABLE_USER_API_KEYS=false
          # Reicht die X-OpenWebUI-Header ans Modell-Backend durch, damit ein
          # Missions-Ergebnis in genau den Ursprungs-Chat zurueckgeht.
          - ENABLE_FORWARD_USER_INFO_HEADERS=true
          # OpenWebUI kappt Modell-Streams nach diesem Wert (Code-Default 300).
          # 900s geben dem Mission-Follow-Stream Luft.
          - AIOHTTP_CLIENT_TIMEOUT=900
          # --- Voice in der WebUI: STT/TTS auf die internen Dienste routen.
          # ACHTUNG (ConfigVar): diese AUDIO_*-Werte greifen NUR beim ersten Start
          # mit frischem Volume. Sonst einmalig in Admin -> Settings -> Audio setzen.
          # SPLIT_ON=punctuation vertont Satz fuer Satz -> erster Ton nach ~1 Satz.
          - AUDIO_STT_ENGINE=openai
          - AUDIO_STT_OPENAI_API_BASE_URL=http://stt-service:8003/v1
          - AUDIO_STT_OPENAI_API_KEY=${OPENAI_API_KEY}
          - AUDIO_STT_MODEL=whisper-1
          - AUDIO_TTS_ENGINE=openai
          - AUDIO_TTS_OPENAI_API_BASE_URL=http://tts-service:8002/v1
          - AUDIO_TTS_OPENAI_API_KEY=${OPENAI_API_KEY}
          - AUDIO_TTS_MODEL=tts-1
          - AUDIO_TTS_VOICE=alloy
          - AUDIO_TTS_SPLIT_ON=none
        volumes:
          - open_webui_data:/app/backend/data
          - ./API_Tokens/webui_secret_key.txt:/run/secrets/webui_secret_key:ro
        depends_on:
          rag-backend:
            condition: service_healthy
          sglang:
            condition: service_healthy
        healthcheck:
          test: ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"]
          interval: 30s
          timeout: 10s
          retries: 5
          start_period: 60s

      # Lokales LLM-Tracing. Nimmt OTLP entgegen und zeigt den Aufruf-Baum je
      # Anfrage. Version UND Digest gepinnt.
      phoenix:
        image: arizephoenix/phoenix:version-19.15.0@sha256:7e06ec56cd7272c855d045013bcbd21edf63144591073dff83d53b351fc66d34
        restart: unless-stopped
        networks:
          - backend
        ports:
          # Nur localhost. 6006 ist UI UND OTLP-HTTP-Endpoint.
          - "127.0.0.1:6006:6006"
        environment:
          - PHOENIX_WORKING_DIR=/mnt/data
          # Ab 19.15 ist die Nutzungstelemetrie an. Aus: das Tracing liegt
          # bewusst lokal, sonst waere LangSmith die einfachere Wahl gewesen.
          - PHOENIX_TELEMETRY_ENABLED=false
        volumes:
          - phoenix_data:/mnt/data
        security_opt:
          - no-new-privileges:true
        cap_drop:
          - ALL
        mem_limit: 2g
        cpus: "2.0"

    volumes:
      pg_data:
      qdrant_data:
      open_webui_data:
      whisper_cache:
      phoenix_data:
    """)
    writefile("docker-compose.yml", COMPOSE, "docker compose")

    # ---------------------------------------------------------------------------
    # Dockerfiles
    # Base-Images per @sha256-Digest gepinnt: rollende Tags invalidieren sonst
    # alle Folge-Layer bei jedem Rebuild. Aktualisieren: Tag hochziehen, Digest via
    #   docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'
    # ---------------------------------------------------------------------------
    writefile("rag_backend/Dockerfile.api", textwrap.dedent("""\
        FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a
        WORKDIR /app
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends build-essential gcc curl wget locales \\
            && sed -i '/^# *en_US.UTF-8 UTF-8/s/^# //' /etc/locale.gen \\
            && locale-gen \\
            && rm -rf /var/lib/apt/lists/*
        RUN pip install --upgrade pip uv
        COPY pyproject.toml .
        RUN --mount=type=cache,target=/root/.cache/uv \\
            uv pip install --system --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu -r pyproject.toml
        COPY . /app/rag_backend
        RUN useradd -m -u 1000 ragbackend && chown -R ragbackend:ragbackend /app
        USER ragbackend
        ENV PYTHONPATH=/app \\
            PYTHONIOENCODING=utf-8 \\
            LANG=C.UTF-8 \\
            LC_ALL=C.UTF-8
        CMD ["uvicorn", "rag_backend.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
    """), "Dockerfile backend")

    writefile("rag_backend/.dockerignore", textwrap.dedent("""\
        __pycache__/
        *.pyc
        *.pyo
        *.pyd
        .pytest_cache/
        test.db
        test_documents_to_ingest/
        .env
        .env.bak.*
        API_Tokens/
        tests/
    """), "rag_backend dockerignore")

    writefile("Dockerfile.qdrant", textwrap.dedent("""\
        FROM qdrant/qdrant:v1.19.0@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc
        USER 0
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends curl \\
            && rm -rf /var/lib/apt/lists/*
    """), "Dockerfile qdrant")

    writefile("Dockerfile.blackwell", textwrap.dedent("""\
        # NICHT auf 0.5.17 hochziehen ohne Speicherplan: ab 0.5.17 legt SGLang fuer
        # Qwen3.5 (Hybrid/GDN) einen Mamba-State-Cache an. Auf 16 GB bleiben nach
        # den 13 GB Gewichten und der 1-GB-CUDA-IPC-Reservierung fuer Multimodal
        # 0,18 GB -- der Scheduler bricht beim Start ab (max_mamba_cache_size=0).
        FROM lmsysorg/sglang:v0.5.12-cu130@sha256:42194170546745092e74cd5f81ad32a7c6e944c7111fe7bf13588152277ff356
        ENV TORCH_CUDA_ARCH_LIST="12.0"
        USER 0
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends curl git \\
            && rm -rf /var/lib/apt/lists/*
        RUN pip install --no-cache-dir --ignore-installed cryptography==50.0.0
        RUN cat >/usr/local/bin/decrypt_secret.py <<'PY'
        import sys
        from pathlib import Path
        from cryptography.fernet import Fernet

        value = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
        if value.startswith("ENC:"):
            key = Path("/run/secrets/fernet_key").read_bytes().strip()
            value = Fernet(key).decrypt(value[4:].encode()).decode()
        print(value)
        PY
    """), "Dockerfile Blackwell/SGLang")

    Path("calc_sandbox").mkdir(exist_ok=True)

    writefile("calc_sandbox/Dockerfile.sandbox", textwrap.dedent("""\
        FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a
        WORKDIR /app
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends ca-certificates wget libicu-dev \\
            && wget -q https://github.com/PowerShell/PowerShell/releases/download/v7.4.18/powershell-7.4.18-linux-x64.tar.gz \\
            && echo "21962bfc832119fc8a58e5eba24bc48f0d31707ce94a4e48a90178a223eba619  powershell-7.4.18-linux-x64.tar.gz" | sha256sum -c - \\
            && mkdir -p /opt/microsoft/powershell/7 \\
            && tar zxf powershell-7.4.18-linux-x64.tar.gz -C /opt/microsoft/powershell/7 \\
            && chmod +x /opt/microsoft/powershell/7/pwsh \\
            && ln -s /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh \\
            && rm powershell-7.4.18-linux-x64.tar.gz \\
            && rm -rf /var/lib/apt/lists/*
        RUN pip install --upgrade pip
        COPY app.py /app/app.py
        RUN pip install --no-cache-dir fastapi uvicorn sympy pandas tabulate openpyxl
        RUN useradd -m -u 1000 sandbox && chown -R sandbox:sandbox /app
        USER sandbox
        CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
    """), "Dockerfile calc-sandbox")

    writefile("calc_sandbox/app.py", textwrap.dedent("""\
        import asyncio
        from fastapi import FastAPI
        from pydantic import BaseModel
        import sympy
        from sympy import E, pi, I, oo
        from sympy.parsing.sympy_parser import (
            parse_expr, standard_transformations, implicit_multiplication_application,
        )

        app = FastAPI(title="Calc Sandbox")

        _SYMPY_TRANSFORMATIONS = standard_transformations + (implicit_multiplication_application,)

        _SAFE_SYMPY_NAMES = {
            "solve", "simplify", "symbols", "diff", "integrate", "limit", "series",
            "expand", "factor", "apart", "together", "cancel", "trigsimp", "sqrt",
            "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
            "exp", "log", "ln", "Abs", "floor", "ceiling", "gcd", "lcm",
            "Matrix", "Rational", "Integer", "Float", "Symbol", "Eq",
            "Sum", "Product", "factorial", "binomial",
        }
        _SAFE_LOCAL_DICT = {name: getattr(sympy, name) for name in _SAFE_SYMPY_NAMES if hasattr(sympy, name)}
        _SAFE_LOCAL_DICT.update({"E": E, "pi": pi, "I": I, "oo": oo})

        class CalcRequest(BaseModel):
            expression: str

        import re as _re
        _DUNDER_RE = _re.compile(r"__\\w+__")
        _FORBIDDEN_NAMES = {
            "eval", "exec", "compile", "open", "input", "exit", "quit",
            "globals", "locals", "vars", "dir", "getattr", "setattr",
            "delattr", "hasattr", "__import__", "breakpoint", "help",
        }

        def _run(expr: str) -> str:
            expr = expr.strip()
            if not expr:
                return "Berechnungsfehler: Leerer Ausdruck."
            if len(expr) > 2000:
                return "Berechnungsfehler: Ausdruck zu lang."
            if _DUNDER_RE.search(expr):
                return "Berechnungsfehler: Dunder-Attribute nicht erlaubt."
            tokens = set(_re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
            forbidden = tokens & _FORBIDDEN_NAMES
            if forbidden:
                return f"Berechnungsfehler: Verbotene Namen: {sorted(forbidden)}"

            _PREFIX_LABELS = {
                "solve(":     "Loesung",
                "diff(":      "Ableitung",
                "integrate(": "Integral",
                "simplify(":  "Vereinfacht",
            }
            label = next((v for k, v in _PREFIX_LABELS.items() if expr.startswith(k)), "Ergebnis")

            try:
                parsed = parse_expr(
                    expr,
                    transformations=_SYMPY_TRANSFORMATIONS,
                    local_dict=_SAFE_LOCAL_DICT,
                    global_dict={},
                    evaluate=True,
                )
                evaled = parsed.evalf() if hasattr(parsed, "evalf") else parsed
                if hasattr(evaled, "is_number") and evaled.is_number:
                    # float() nur fuer reelle Zahlen: komplexe Ergebnisse haben
                    # ebenfalls is_number=True und wuerden als Fehler gemeldet.
                    if getattr(evaled, "is_real", False):
                        return f"{label}: {float(evaled):.10g}"
                    return f"{label}: {evaled}"
                return f"{label}: {evaled}"
            except (SyntaxError, TypeError, ValueError, AttributeError, NameError) as e:
                return f"Berechnungsfehler: {str(e)}"
            except Exception as e:
                return f"Berechnungsfehler: {type(e).__name__}: {str(e)}"

        @app.post("/calculate")
        async def calculate(req: CalcRequest):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_run, req.expression),
                    timeout=5.0,
                )
                if len(result) > 10000:
                    result = result[:10000] + "... [gekuerzt]"
                return {"result": result}
            except asyncio.TimeoutError:
                return {"result": "Berechnungsfehler: Timeout nach 5 Sekunden (Ausdruck zu komplex)."}
            except Exception as e:
                return {"result": f"Berechnungsfehler: {str(e)}"}

        import os
        import signal
        import subprocess
        import sys
        import resource

        _EXEC_OUTPUT_CAP = 20000
        _EXEC_CODE_CAP = 20000


        def _pwsh_rlimits():
            # Prozess-Limits fuer den pwsh-Kindprozess. RLIMIT_NPROC und
            # RLIMIT_FSIZE bewusst NICHT: der .NET-Threadpool reisst ein niedriges
            # NPROC, und .NET schreibt beim Start >16 MiB. Beides deckelt der
            # Container (pids_limit, tmpfs size).
            resource.setrlimit(resource.RLIMIT_CPU, (12, 13))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        # Runner laeuft als isolierter Kindprozess (python -I -B) mit eigenen
        # rlimits als Defense-in-Depth ueber die Container-Limits hinaus.
        _EXEC_RUNNER = chr(10).join([
            "import resource, sys",
            "resource.setrlimit(resource.RLIMIT_CPU, (12, 13))",
            "resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))",
            "resource.setrlimit(resource.RLIMIT_FSIZE, (16777216, 16777216))",
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))",
            "import json, csv, re, math, statistics, collections, itertools, datetime",
            "try:",
            "    import pandas as pd",
            "except Exception:",
            "    pd = None",
            "try:",
            "    from tabulate import tabulate",
            "except Exception:",
            "    tabulate = None",
            "_ns = {'__name__': '__main__', 'pd': pd, 'tabulate': tabulate, 'json': json, 'csv': csv, 're': re, 'math': math, 'statistics': statistics, 'collections': collections, 'itertools': itertools, 'datetime': datetime}",
            "_src = sys.stdin.buffer.read().decode('utf-8', 'replace')",
            "exec(compile(_src, '<execute_code>', 'exec'), _ns)",
        ])


        class ExecRequest(BaseModel):
            code: str
            language: str = "python"
            timeout: float = 15.0


        def _exec_code(code: str, language: str, timeout: float) -> dict:
            code = (code or "").strip()
            if not code:
                return {"error": "Leerer Code."}
            if len(code) > _EXEC_CODE_CAP:
                return {"error": f"Code zu lang (max {_EXEC_CODE_CAP} Zeichen)."}
            try:
                t = max(1.0, min(float(timeout or 15.0), 30.0))
            except Exception:
                t = 15.0
            
            if language.lower() == "powershell":
                import tempfile
                import os
                fd, path = tempfile.mkstemp(suffix=".ps1", dir="/tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        # $ErrorActionPreference='Stop' macht non-terminating
                        # Cmdlet-Fehler terminierend -> rc != 0.
                        f.write("$ErrorActionPreference = 'Stop'" + chr(10) + code)
                    proc = subprocess.Popen(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-File", path],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        start_new_session=True, cwd="/tmp",
                        # NO_COLOR/TERM=dumb: sonst landen ANSI-Farbcodes im
                        # issue_description.
                        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                             "NO_COLOR": "1", "TERM": "dumb"},
                        preexec_fn=_pwsh_rlimits,
                    )
                    try:
                        out, err = proc.communicate(timeout=t)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            pass
                        try:
                            proc.communicate(timeout=5)
                        except Exception:
                            pass
                        return {"error": f"Timeout nach {t:.0f}s (Sandbox-Prozess hart beendet)."}
                    stdout = out.decode("utf-8", "replace")
                    stderr = err.decode("utf-8", "replace")
                    if len(stdout) > _EXEC_OUTPUT_CAP:
                        stdout = stdout[:_EXEC_OUTPUT_CAP] + chr(10) + "... [gekuerzt]"
                    if proc.returncode != 0:
                        msg = stderr.strip() or "Unbekannter Ausfuehrungsfehler."
                        if stdout.strip():
                            msg = msg + chr(10) + "--- stdout ---" + chr(10) + stdout
                        return {"error": msg[:_EXEC_OUTPUT_CAP]}
                    # rc==0, aber Warnungen auf stderr: sichtbar ans Ergebnis haengen.
                    if stderr.strip():
                        stdout = (stdout + chr(10) + "--- stderr ---" + chr(10) + stderr)[:_EXEC_OUTPUT_CAP]
                    return {"result": stdout}
                finally:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            else:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-B", "-c", _EXEC_RUNNER],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, cwd="/tmp",
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                         "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
                )
                try:
                    out, err = proc.communicate(input=code.encode("utf-8"), timeout=t)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    try:
                        proc.communicate(timeout=5)
                    except Exception:
                        pass
                    return {"error": f"Timeout nach {t:.0f}s (Sandbox-Prozess hart beendet)."}
                stdout = out.decode("utf-8", "replace")
                stderr = err.decode("utf-8", "replace")
                if len(stdout) > _EXEC_OUTPUT_CAP:
                    stdout = stdout[:_EXEC_OUTPUT_CAP] + chr(10) + "... [gekuerzt]"
                if proc.returncode != 0:
                    msg = stderr.strip() or "Unbekannter Ausfuehrungsfehler."
                    if stdout.strip():
                        msg = msg + chr(10) + "--- stdout ---" + chr(10) + stdout
                    return {"error": msg[:_EXEC_OUTPUT_CAP]}
                return {"result": stdout}


        @app.post("/execute")
        async def execute(req: ExecRequest):
            # Echter Sicherheitsrahmen = Container: non-root, cap_drop ALL,
            # no-new-privileges, read_only rootfs, Netz ohne Egress, /app/data :ro.
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_exec_code, req.code, req.language, req.timeout),
                    timeout=min(float(req.timeout or 15.0), 30.0) + 10.0,
                )
            except asyncio.TimeoutError:
                return {"error": "Sandbox-Watchdog-Timeout."}
            except Exception as e:
                return {"error": f"Sandbox-Fehler: {type(e).__name__}: {e}"}

        @app.get("/healthz")
        async def health():
            return {"status": "ok"}
    """), "calc_sandbox app")


    # ---------------------------------------------------------------------------
    # tts-service (Qwen3-TTS 1.7B via qwentts.cpp, CPU-only + BLAS)
    #   Beide Modi laufen ueber den residenten tts-server (Modell bleibt geladen):
    #   - Preset (Default): CustomVoice-Modell, Sprecher via QWEN_VOICE/-DEFAULT.
    #   - Clone (QWEN_CLONE_REF_* gesetzt): tts-server-PATCH laedt die Referenzstimme
    #     EINMALIG beim Start (Base-Modell) und nutzt sie fuer alle Anfragen -> warm,
    #     ~2x schneller als CLI-per-Call. Patch = tts_service/tts-server.patched.cpp.
    # Modell + qwentts.cpp auf Commits gepinnt (reproduzierbar). Modelle in eigener,
    # frueher Layer -> ein C++-Build-Fix unten laedt die ~5 GB GGUF nicht erneut.
    # ---------------------------------------------------------------------------
    Path("tts_service").mkdir(exist_ok=True)

    # Upstream-Patch fuer tools/tts-server.cpp: die Flags --ref-wav/--ref-spk/
    # --ref-rvq/--ref-text laden EINE Referenzstimme einmalig und binden sie in
    # jede Synthese. Ohne diese Flags == Upstream.
    TTS_SERVER_PATCHED_CPP = r'''
// tts-server.cpp (Argus-Patch zu ServeurpersoCom/qwentts.cpp @0bf4a18)
//
// Upstream: https://github.com/ServeurpersoCom/qwentts.cpp -- MIT License
// Copyright (c) 2023-2026 The omnivoice.cpp authors
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
//
// Aenderung ggue. Upstream: laedt optional EINE Referenzstimme (Base-Modell,
// --ref-wav/--ref-spk/--ref-rvq/--ref-text) EINMALIG beim Start und nutzt sie
// fuer ALLE Anfragen -> geklonte Stimme bei resident geladenem Modell (warm).
// Ohne Ref-Flags identisch zum Original (CustomVoice + named speaker).
#include "tts-server.h"

#include "audio-io.h"
#include "qwen.h"
#include "rvq-file.h"
#include "version.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <random>
#include <string>
#include <vector>

#ifndef _WIN32
#include <sys/mman.h>
#include <cerrno>
#endif

static const int RVQ_CODE_BITS = 11;  // 11 bits/code (V <= 2048), wie qwen-codec

static void print_usage(const char * prog) {
    fprintf(stderr, "qwentts.cpp %s\n\n", QWEN_VERSION);
    fprintf(stderr,
            "Usage: %s --model <gguf> --codec <gguf> [options]\n\n"
            "Required:\n"
            "  --model <gguf>          Talker LM GGUF (qwen-talker-*.gguf)\n"
            "  --codec <gguf>          Codec GGUF (qwen-tokenizer-*.gguf)\n\n"
            "Optional:\n"
            "  --host <ip>             Listen address (default: 127.0.0.1)\n"
            "  --port <n>              Listen port (default: 8080)\n"
            "  --lang <name>           Language label (default: auto)\n"
            "  --ref-wav <path>        Reference WAV for a cloned voice (Base model only)\n"
            "  --ref-spk <path>        Pre-extracted speaker embedding (qwen-codec)\n"
            "  --ref-rvq <path>        Pre-encoded ICL reference codes (needs --ref-spk + --ref-text)\n"
            "  --ref-text <path>       Transcript of the reference (enables ICL clone)\n"
            "  --seed <int>            Fixed sampling seed for all requests (default: -1 = random per call)\n"
            "  --temp <f>              Talker + sub-talker temperature (default: -1 = keep model default 0.9)\n"
            "  --no-fa                 Disable flash attention\n"
            "  --clamp-fp16            Clamp hidden states to FP16 range\n",
            prog);
}

static std::string basename_of(const char * path) {
    std::string s = path;
    size_t      p = s.find_last_of("/\\");
    return p == std::string::npos ? s : s.substr(p + 1);
}

// .spk: rohe f32-Werte, Anzahl == Embedding-Dimension (wie qwen-tts.cpp).
static bool read_spk_file(const char * path, std::vector<float> & emb) {
    FILE * f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[Server] ERROR: cannot open --ref-spk '%s'\n", path); return false; }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || (sz % (long) sizeof(float)) != 0) {
        fprintf(stderr, "[Server] ERROR: --ref-spk '%s' size %ld not a multiple of 4\n", path, sz);
        fclose(f);
        return false;
    }
    emb.resize((size_t) sz / sizeof(float));
    if (fread(emb.data(), sizeof(float), emb.size(), f) != emb.size()) {
        fprintf(stderr, "[Server] ERROR: short read on --ref-spk '%s'\n", path);
        fclose(f);
        return false;
    }
    fclose(f);
    return true;
}

static bool read_text_file(const char * path, std::string & out) {
    FILE * f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[Server] ERROR: cannot open '%s'\n", path); return false; }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz < 0) { fclose(f); return false; }
    out.resize((size_t) sz);
    if (sz > 0 && fread(&out[0], 1, (size_t) sz, f) != (size_t) sz) { fclose(f); return false; }
    fclose(f);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
    return true;
}

int main(int argc, char ** argv) {
    const char *  talker_path   = NULL;
    const char *  codec_path    = NULL;
    const char *  ref_wav_path  = NULL;
    const char *  ref_spk_path  = NULL;
    const char *  ref_rvq_path  = NULL;
    const char *  ref_text_path = NULL;
    std::string   lang          = "auto";
    server_config cfg;
    bool          use_fa     = true;
    bool          clamp_fp16 = false;
    // Argus-Patch: fester Seed / Temperatur ueber alle Anfragen. seed >= 0 reseedet
    // qt_synthesize pro Request identisch -> jeder Satz startet aus demselben RNG-Zustand
    // (kein Stimm-Drift beim satzweisen Streaming). -1 == Upstream (Hardware-Random pro Call).
    int64_t       cli_seed   = -1;
    float         cli_temp   = -1.0f;

    for (int i = 1; i < argc; i++) {
        const char * arg = argv[i];
        if (!std::strcmp(arg, "--model") && i + 1 < argc) {
            talker_path = argv[++i];
        } else if (!std::strcmp(arg, "--codec") && i + 1 < argc) {
            codec_path = argv[++i];
        } else if (!std::strcmp(arg, "--host") && i + 1 < argc) {
            cfg.host = argv[++i];
        } else if (!std::strcmp(arg, "--port") && i + 1 < argc) {
            cfg.port = std::atoi(argv[++i]);
        } else if (!std::strcmp(arg, "--lang") && i + 1 < argc) {
            lang = argv[++i];
        } else if (!std::strcmp(arg, "--ref-wav") && i + 1 < argc) {
            ref_wav_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-spk") && i + 1 < argc) {
            ref_spk_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-rvq") && i + 1 < argc) {
            ref_rvq_path = argv[++i];
        } else if (!std::strcmp(arg, "--ref-text") && i + 1 < argc) {
            ref_text_path = argv[++i];
        } else if (!std::strcmp(arg, "--seed") && i + 1 < argc) {
            cli_seed = (int64_t) std::atoll(argv[++i]);
        } else if (!std::strcmp(arg, "--temp") && i + 1 < argc) {
            cli_temp = (float) std::atof(argv[++i]);
        } else if (!std::strcmp(arg, "--no-fa")) {
            use_fa = false;
        } else if (!std::strcmp(arg, "--clamp-fp16")) {
            clamp_fp16 = true;
        } else if (!std::strcmp(arg, "--help") || !std::strcmp(arg, "-h")) {
            print_usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "[CLI] ERROR: unknown arg: %s\n", arg);
            print_usage(argv[0]);
            return 1;
        }
    }

    if (!talker_path || !codec_path) {
        print_usage(argv[0]);
        return 0;
    }

    struct qt_init_params iparams;
    qt_init_default_params(&iparams);
    iparams.talker_path = talker_path;
    iparams.codec_path  = codec_path;
    iparams.use_fa      = use_fa;
    iparams.clamp_fp16  = clamp_fp16;

    struct qt_context * q = qt_init(&iparams);
    if (!q) {
        fprintf(stderr, "[Server] FATAL: %s\n", qt_last_error());
        return 1;
    }

#ifndef _WIN32
    if (mlockall(MCL_CURRENT) == 0) {
        fprintf(stderr, "[Server] Memory-pinning active (MCL_CURRENT)\n");
    } else {
        fprintf(stderr, "[Server] WARN: mlockall failed (errno=%d: %s) -- check memlock ulimit\n",
                errno, strerror(errno));
    }
#endif

    // --- Referenzstimme EINMALIG laden (Lade-Logik aus qwen-tts.cpp) ---
    std::string  ref_text_buf;
    const char * ref_text = NULL;
    if (ref_text_path) {
        if (!read_text_file(ref_text_path, ref_text_buf)) { qt_free(q); return 1; }
        if (!ref_text_buf.empty()) ref_text = ref_text_buf.c_str();
    }
    std::unique_ptr<float, void (*)(void *)> raw_holder(NULL, std::free);
    const float * ref_audio_24k = NULL;
    int           ref_n_samples = 0;
    if (ref_wav_path) {
        int     T_in = 0;
        float * raw  = audio_read_mono(ref_wav_path, 24000, &T_in);
        if (!raw || T_in <= 0) {
            fprintf(stderr, "[Server] FATAL: cannot read --ref-wav '%s'\n", ref_wav_path);
            if (raw) std::free(raw);
            qt_free(q);
            return 1;
        }
        raw_holder.reset(raw);
        ref_audio_24k = raw;
        ref_n_samples = T_in;
    }
    std::vector<float>   ref_spk_emb;
    std::vector<int32_t> ref_codes;
    int                  ref_T = 0;
    if (ref_spk_path) {
        if (!read_spk_file(ref_spk_path, ref_spk_emb)) { qt_free(q); return 1; }
        fprintf(stderr, "[Server] Reference SPK: %s, %zu f32 values\n", ref_spk_path, ref_spk_emb.size());
    }
    if (ref_rvq_path) {
        const int K = qt_num_codebooks(q);
        if (!rvq_read_file(ref_rvq_path, K, RVQ_CODE_BITS, ref_codes, &ref_T)) { qt_free(q); return 1; }
        fprintf(stderr, "[Server] Reference RVQ: %s, K=%d T=%d\n", ref_rvq_path, K, ref_T);
    }

    // Vorab in einfache, by-value capturebare Werte aufloesen. Die Buffer
    // (raw_holder, ref_text_buf/_spk_emb/_codes) leben in main und ueberdauern tts_server_run.
    const float *   cap_ref_audio = ref_audio_24k;
    const int       cap_ref_ns    = ref_n_samples;
    const char *    cap_ref_text  = ref_text;
    const float *   cap_ref_spk   = ref_spk_emb.empty() ? NULL : ref_spk_emb.data();
    const int       cap_ref_spk_d = (int) ref_spk_emb.size();
    const int32_t * cap_ref_codes = ref_codes.empty() ? NULL : ref_codes.data();
    const int       cap_ref_T     = ref_T;
    const bool      clone_mode    = (cap_ref_audio || cap_ref_spk || cap_ref_codes);
    if (clone_mode) {
        fprintf(stderr, "[Server] Voice-Clone aktiv (Referenz beim Start geladen) -- named speakers deaktiviert.\n");
    }

    tts_backend be;
    be.model_id = basename_of(talker_path);
    if (clone_mode) {
        be.voices.push_back("clone");
    } else {
        int n = qt_n_speakers(q);
        for (int i = 0; i < n; i++) {
            be.voices.push_back(qt_speaker_name(q, i));
        }
    }

    be.synthesize = [q, &lang, clone_mode, cap_ref_audio, cap_ref_ns, cap_ref_text,
                     cap_ref_spk, cap_ref_spk_d, cap_ref_codes, cap_ref_T, cli_seed, cli_temp]
                    (const tts_request & req, const tts_sink & sink, std::string & err) -> int {
        struct qt_tts_params p;
        qt_tts_default_params(&p);
        p.text = req.input.c_str();
        p.lang = lang.c_str();
        // Argus-Patch: feste Sampling-Parameter -> stabile Stimme ueber alle Saetze.
        int64_t resolved_seed = cli_seed;
        if (resolved_seed < 0) {
            std::random_device rd;
            resolved_seed = (int64_t) (((uint64_t) rd() << 32) ^ (uint64_t) rd());
            if (resolved_seed < 0) {
                resolved_seed = -resolved_seed;
            }
        }
        p.seed = resolved_seed;
        fprintf(stderr, "[Server] Synthesizing request with seed: %lld (text: \"%s\")\n",
                (long long) resolved_seed, req.input.c_str());

        if (cli_temp >= 0.0f) {
            p.temperature           = cli_temp;
            p.subtalker_temperature = cli_temp;
        }
        if (clone_mode) {
            // Geklonte Stimme statt named speaker (Base-Modell lehnt speaker/instruct ab).
            p.ref_audio_24k = cap_ref_audio;
            p.ref_n_samples = cap_ref_ns;
            p.ref_text      = cap_ref_text;
            p.ref_spk_emb   = cap_ref_spk;
            p.ref_spk_dim   = cap_ref_spk_d;
            p.ref_codes     = cap_ref_codes;
            p.ref_T         = cap_ref_T;
        } else {
            if (!req.voice.empty() && qt_n_speakers(q) > 0) {
                p.speaker = req.voice.c_str();
            }
            if (!req.instructions.empty()) {
                p.instruct = req.instructions.c_str();
            }
        }

        const tts_sink * sink_ptr = &sink;
        p.on_chunk                = [](const float * s, int ns, void * u) -> bool {
            return (*static_cast<const tts_sink *>(u))(s, ns);
        };
        p.on_chunk_user_data = (void *) sink_ptr;

        struct qt_audio out = {};
        enum qt_status  rc  = qt_synthesize(q, &p, &out);
        qt_audio_free(&out);
        if (rc != QT_STATUS_OK) {
            err = qt_last_error();
            return (int) rc;
        }
        return 0;
    };

    int rc = tts_server_run(be, cfg);
    qt_free(q);
    return rc;
}
'''
    writefile("tts_service/tts-server.patched.cpp", TTS_SERVER_PATCHED_CPP, "tts-server patch", do_dedent=False)

    PATCH_BACKEND_PY = r'''import re

p = 'src/backend.h'
content = open(p, 'r', encoding='utf-8').read()

old = """static int backend_cpu_n_threads(void) {
    int n = (int) std::thread::hardware_concurrency() / 2;
    return n > 0 ? n : 1;
}"""

new = """static int backend_cpu_n_threads(void) {
    if (const char * env = std::getenv("GGML_NUM_THREADS")) { int val = std::atoi(env); if (val > 0) return val; }
    if (const char * env = std::getenv("OMP_NUM_THREADS")) { int val = std::atoi(env); if (val > 0) return val; }
    int n = (int) std::thread::hardware_concurrency() / 2;
    return n > 0 ? n : 1;
}"""

if old in content:
    open(p, 'w', encoding='utf-8').write(content.replace(old, new))
    print("Replaced exact string!")
else:
    content = re.sub(
        r'static int backend_cpu_n_threads\(void\)\s*\{\s*int n = \(int\) std::thread::hardware_concurrency\(\) / 2;\s*return n > 0 \? n : 1;\s*\}',
        new,
        content
    )
    open(p, 'w', encoding='utf-8').write(content)
    print("Replaced via regex!")
'''
    writefile("tts_service/patch_backend.py", PATCH_BACKEND_PY, "backend.h patch script", do_dedent=False)

    writefile("tts_service/Dockerfile.tts", textwrap.dedent("""\
        FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a
        WORKDIR /app
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends \\
                git cmake build-essential libopenblas-dev pkg-config \\
                ffmpeg wget ca-certificates \\
            && rm -rf /var/lib/apt/lists/*
        RUN pip install --no-cache-dir fastapi uvicorn httpx

        # GGUF-Modelle ZUERST (grosse, stabile Layer). Gepinnt auf einen Repo-Commit.
        ARG GGUF_COMMIT=e0f336a048a3de02b29b8ad92969217d9ecffe3e
        ARG GGUF_URL=https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF/resolve/${GGUF_COMMIT}
        RUN mkdir -p /app/models \\
            && wget -qO /app/models/qwen-tokenizer-12hz-Q8_0.gguf          "${GGUF_URL}/qwen-tokenizer-12hz-Q8_0.gguf" \\
            && wget -qO /app/models/qwen-talker-1.7b-customvoice-Q8_0.gguf "${GGUF_URL}/qwen-talker-1.7b-customvoice-Q8_0.gguf" \\
            && wget -qO /app/models/qwen-talker-1.7b-base-Q8_0.gguf        "${GGUF_URL}/qwen-talker-1.7b-base-Q8_0.gguf"

        # qwentts.cpp CPU-only + BLAS, statisch gelinkt, auf Commit gepinnt.
        # Vor dem Build wird tools/tts-server.cpp durch den Argus-Patch ersetzt
        COPY tts-server.patched.cpp /tmp/tts-server.patched.cpp
        COPY patch_backend.py /tmp/patch_backend.py
        ARG QWENTTS_COMMIT=0bf4a18b22e8bb8718d95294e9f7f45c0d4270a4
        RUN git clone https://github.com/ServeurpersoCom/qwentts.cpp.git /build/qwentts \\
            && cd /build/qwentts \\
            && git checkout ${QWENTTS_COMMIT} \\
            && git submodule update --init --recursive \\
            && cp /tmp/tts-server.patched.cpp tools/tts-server.cpp \\
            && python3 /tmp/patch_backend.py \\
            && cmake -S . -B build -DGGML_BLAS=ON -DGGML_OPENMP=OFF -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release \\
            && cmake --build build --config Release -j"$(nproc)" \\
            && find build -maxdepth 3 -type f '(' -name tts-server -o -name qwen-tts -o -name qwen-codec ')' -exec cp {} /usr/local/bin/ ';' \\
            && ( find build -maxdepth 3 -name 'lib*.so*' -exec cp -P {} /usr/local/lib/ ';' || true ) \\
            && ldconfig \\
            && rm -rf /build

        COPY app.py /app/app.py
        # KEIN 'chown -R /app': das kopiert die 4,4 GB GGUF-Modelle in einen
        # zweiten Layer. ttsuser braucht /app nur lesend.
        RUN useradd -m -u 1000 ttsuser
        USER ttsuser
        ENV QWEN_TALKER=/app/models/qwen-talker-1.7b-customvoice-Q8_0.gguf \\
            QWEN_BASE_TALKER=/app/models/qwen-talker-1.7b-base-Q8_0.gguf \\
            QWEN_CODEC=/app/models/qwen-tokenizer-12hz-Q8_0.gguf \\
            QWEN_LANG=auto \\
            QWEN_VOICE= \\
            QWEN_SERVER_PORT=8080 \\
            OMP_NUM_THREADS=1 \\
            GGML_NUM_THREADS=6 \\
            OPENBLAS_NUM_THREADS=1
        CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
    """), "Dockerfile tts-service")

    writefile("tts_service/app.py", textwrap.dedent('''\
        import asyncio
        import hashlib
        import io
        import os
        import re
        import subprocess
        import tempfile
        import wave
        import time
        from collections import deque, OrderedDict
        from contextlib import asynccontextmanager
        from pathlib import Path

        import httpx
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
        from pydantic import BaseModel

        # --- Konfiguration (alle via docker-compose/.env ueberschreibbar) ---
        QWEN_TALKER = os.getenv("QWEN_TALKER", "/app/models/qwen-talker-1.7b-customvoice-Q8_0.gguf")
        QWEN_BASE_TALKER = os.getenv("QWEN_BASE_TALKER", "/app/models/qwen-talker-1.7b-base-Q8_0.gguf")
        QWEN_CODEC = os.getenv("QWEN_CODEC", "/app/models/qwen-tokenizer-12hz-Q8_0.gguf")
        QWEN_LANG = os.getenv("QWEN_LANG", "auto")
        QWEN_VOICE = os.getenv("QWEN_VOICE", "").strip()
        # Pflicht-Fallback fuer den Server-Modus: das CustomVoice-Modell braucht
        # immer einen Sprecher.
        QWEN_DEFAULT_VOICE = os.getenv("QWEN_DEFAULT_VOICE", "ryan").strip() or "ryan"
        QWEN_SERVER_PORT = int(os.getenv("QWEN_SERVER_PORT", "8080"))
        # Gueltige Preset-Sprecher. Open WebUI schickt OpenAI-Stimmnamen ->
        # im Preset-Modus auf einen bekannten Sprecher normalisieren.
        QWEN_KNOWN_VOICES = {"serena", "vivian", "uncle_fu", "ryan", "aiden",
                             "ono_anna", "sohee", "eric", "dylan"}

        # Clone-Modus (WARM): sind Referenz-Dateien gesetzt, laedt der tts-server
        # sie einmalig beim Start und nutzt sie fuer alle Anfragen.
        # Schneller ohne Per-Call-Encode: vorab erzeugte .spk + .rvq statt WAV.
        QWEN_CLONE_REF_WAV = os.getenv("QWEN_CLONE_REF_WAV", "").strip()
        QWEN_CLONE_REF_TEXT = os.getenv("QWEN_CLONE_REF_TEXT", "").strip()
        QWEN_CLONE_REF_SPK = os.getenv("QWEN_CLONE_REF_SPK", "").strip()
        QWEN_CLONE_REF_RVQ = os.getenv("QWEN_CLONE_REF_RVQ", "").strip()
        # Fester Seed ueber alle Anfragen gegen Stimm-Drift. Leer = Upstream
        # (Hardware-Random pro Satz). Empfehlung: QWEN_SEED setzen, QWEN_TEMP leer.
        QWEN_SEED = os.getenv("QWEN_SEED", "").strip()
        QWEN_TEMP = os.getenv("QWEN_TEMP", "").strip()

        def _ref_present(p: str) -> bool:
            return bool(p) and Path(p).is_file()

        # Clone nur aktiv, wenn die Referenzdatei wirklich da ist -- sonst startet
        # der Server im Preset-Modus statt zu sterben.
        CLONE_CONFIGURED = bool(QWEN_CLONE_REF_WAV or QWEN_CLONE_REF_SPK)
        CLONE_MODE = _ref_present(QWEN_CLONE_REF_WAV) or _ref_present(QWEN_CLONE_REF_SPK)

        MAX_INPUT_CHARS = int(os.getenv("TTS_MAX_INPUT_CHARS", "4000"))
        SAMPLE_RATE = 24000
        _synth_lock = asyncio.Lock()
        _server_proc = None
        _http = None

        # --- Markdown/Code aus dem Text fuer die Sprachausgabe entfernen ---
        # ZENTRALE Bereinigung fuer ALLE Voice-Clients: Quellen-Trailer,
        # Markdown-Links -> Linktext, Zahlenbereiche "16-17" -> "16 bis 17".
        _CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
        _INLINE_CODE_RE = re.compile(r"`[^`]+`")
        _URL_RE = re.compile(r"https?://\\S+")
        _MD_HEAD_RE = re.compile(r"^#+\\s*", re.MULTILINE)
        _MD_BULLET_RE = re.compile(r"^[\\s]*[-*]\\s+", re.MULTILINE)
        _MD_BOLD_ITALIC_RE = re.compile(r"\\*+|_+")
        _MULTI_SPACE_RE = re.compile(r"\\s+")
        # Quellen-/URL-Trailer am Antwortende ("Verwendete Quellen: ...") nicht vorlesen.
        _SOURCES_TRAILER_RE = re.compile(
            r"(?is)\\n\\s*(?:verwendete\\s+quellen|verwendete\\s+urls|quellenangaben|quellen|sources)\\s*:.*"
        )
        # Markdown-Link [Text](url) -> nur den Linktext behalten.
        _MD_LINK_RE = re.compile(r"\\[([^\\]]+)\\]\\((?:https?://)?[^)]*\\)")
        # Zahlenbereiche ausschreiben; uebrige Striche -> Leerzeichen.
        _NUM_RANGE_RE = re.compile(r"(\\d)\\s*[-\\u2013\\u2014]\\s*(\\d)")
        _DASH_RE = re.compile(r"[-\\u2013\\u2014]")

        def _strip_for_tts(text: str) -> str:
            t = _SOURCES_TRAILER_RE.sub("", text)
            t = _CODE_BLOCK_RE.sub(" [Codeblock] ", t)
            t = _INLINE_CODE_RE.sub(" ", t)
            t = _MD_LINK_RE.sub(r"\\1", t)
            t = _URL_RE.sub(" ", t)
            t = _MD_HEAD_RE.sub("", t)
            t = _MD_BULLET_RE.sub("", t)
            t = _MD_BOLD_ITALIC_RE.sub("", t)
            t = _NUM_RANGE_RE.sub(r"\\1 bis \\2", t)
            t = _DASH_RE.sub(" ", t)
            t = _MULTI_SPACE_RE.sub(" ", t)
            return t.strip()


        class SpeechRequest(BaseModel):
            input: str
            voice: str | None = None
            # Default mp3 fuer Browser-Clients (Open WebUI). Telegram fordert ogg explizit an.
            response_format: str = "mp3"
            # Optionaler Client-Kurz-Cut, NACH _strip_for_tts satzweise angewendet.
            max_chars: int | None = None


        async def _wait_for_server(timeout: float = 180.0):
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            url = f"http://127.0.0.1:{QWEN_SERVER_PORT}/v1/audio/speech"
            async with httpx.AsyncClient(timeout=30.0) as probe:
                while loop.time() < deadline:
                    if _server_proc is not None and _server_proc.poll() is not None:
                        raise RuntimeError("tts-server-Prozess vorzeitig beendet (siehe Container-Log).")
                    try:
                        # Warmup-Probe: sobald der Server HTTP antwortet, ist das Modell geladen.
                        await probe.post(url, json={"input": "."})
                        return
                    except Exception:
                        await asyncio.sleep(1.5)
            raise RuntimeError("tts-server nicht rechtzeitig bereit.")


        @asynccontextmanager
        async def lifespan(app):
            global _server_proc, _http
            _http = httpx.AsyncClient(timeout=120.0)
            if CLONE_CONFIGURED and not CLONE_MODE:
                print("[tts] WARNUNG: Clone konfiguriert, aber Referenzdatei fehlt "
                      "-> Preset-Modus. voice_ref/ pruefen.", flush=True)
            # tts-server immer resident starten -> Modell bleibt geladen (warm, beide Modi).
            cmd = ["tts-server", "--codec", QWEN_CODEC,
                   "--host", "127.0.0.1", "--port", str(QWEN_SERVER_PORT), "--lang", QWEN_LANG]
            if CLONE_MODE:
                # Base-Modell + Referenzstimme beim Start (Argus-Patch laedt sie einmalig).
                cmd += ["--model", QWEN_BASE_TALKER]
                if QWEN_CLONE_REF_WAV:
                    cmd += ["--ref-wav", QWEN_CLONE_REF_WAV]
                if QWEN_CLONE_REF_SPK:
                    cmd += ["--ref-spk", QWEN_CLONE_REF_SPK]
                if QWEN_CLONE_REF_RVQ:
                    cmd += ["--ref-rvq", QWEN_CLONE_REF_RVQ]
                if QWEN_CLONE_REF_TEXT:
                    cmd += ["--ref-text", QWEN_CLONE_REF_TEXT]
            else:
                cmd += ["--model", QWEN_TALKER]
            # Feste Sampling-Parameter (beide Modi) -> stabile Stimme ueber alle Saetze.
            if QWEN_SEED:
                cmd += ["--seed", QWEN_SEED]
            if QWEN_TEMP:
                cmd += ["--temp", QWEN_TEMP]
            _server_proc = subprocess.Popen(cmd)
            await _wait_for_server()
            yield
            if _server_proc is not None:
                _server_proc.terminate()
                try:
                    _server_proc.wait(timeout=10)
                except Exception:
                    _server_proc.kill()
            if _http is not None:
                await _http.aclose()


        app = FastAPI(title="Argus TTS (Qwen3-TTS / qwentts.cpp)", lifespan=lifespan)


        def _to_wav(audio: bytes) -> bytes:
            if audio[:4] == b"RIFF":
                return audio
            # rohes PCM (s16le 24k mono) in einen WAV-Container packen
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(audio)
            return buf.getvalue()


        def _audio_to_ogg(audio: bytes) -> bytes:
            # qwentts liefert je nach Build WAV (RIFF) oder rohes PCM -> beides abfangen.
            is_wav = audio[:4] == b"RIFF"
            in_args = ["-i", "pipe:0"] if is_wav else ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"]
            cmd = ["ffmpeg", "-y", "-loglevel", "error", *in_args,
                   "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1"]
            proc = subprocess.run(cmd, input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if proc.returncode != 0 or not proc.stdout:
                raise RuntimeError(f"ffmpeg ogg-Konvertierung fehlgeschlagen: {proc.stderr.decode('utf-8','ignore')[:300]}")
            return proc.stdout


        def _audio_to_mp3(audio: bytes) -> bytes:
            # Browser-taugliches Format (Open WebUI). qwentts liefert WAV (RIFF) oder rohes PCM.
            is_wav = audio[:4] == b"RIFF"
            in_args = ["-i", "pipe:0"] if is_wav else ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"]
            cmd = ["ffmpeg", "-y", "-loglevel", "error", *in_args,
                   "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"]
            proc = subprocess.run(cmd, input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if proc.returncode != 0 or not proc.stdout:
                raise RuntimeError(f"ffmpeg mp3-Konvertierung fehlgeschlagen: {proc.stderr.decode('utf-8','ignore')[:300]}")
            return proc.stdout


        async def _synth_server(text: str, voice: str | None) -> bytes:
            payload = {"input": text}
            if not CLONE_MODE:
                # CustomVoice VERLANGT einen gueltigen Sprecher, sonst liefert der
                # Server stumme 0 Bytes. Im Clone-Modus wird voice ignoriert.
                req_voice = (voice or "").strip().lower()
                if req_voice not in QWEN_KNOWN_VOICES:
                    env_voice = QWEN_VOICE.strip().lower()
                    req_voice = env_voice if env_voice in QWEN_KNOWN_VOICES else QWEN_DEFAULT_VOICE
                payload["voice"] = req_voice
            resp = await _http.post(f"http://127.0.0.1:{QWEN_SERVER_PORT}/v1/audio/speech", json=payload)
            resp.raise_for_status()
            return resp.content


        def _get_audio_duration(audio_bytes: bytes) -> float:
            if not audio_bytes:
                return 0.0
            if audio_bytes[:4] == b"RIFF":
                try:
                    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
                        frames = w.getnframes()
                        rate = w.getframerate()
                        if rate > 0:
                            return frames / float(rate)
                except Exception:
                    return max(0.0, (len(audio_bytes) - 44) / 48000.0)
            return len(audio_bytes) / 48000.0


        _MAX_TTS_HISTORY = 100
        _tts_requests = deque(maxlen=_MAX_TTS_HISTORY)
        _tts_requests_total = 0

        # --- Audio-Cache ---
        # Eine Synthese kostet ungefaehr so viel Zeit, wie das Ergebnis spaeter
        # dauert (RTF um 1). Ein zweites "Vorlesen" desselben Absatzes darf das
        # nicht noch einmal bezahlen -- gleicher Text, gleiche Stimme, gleiches
        # Format ergeben Byte fuer Byte dasselbe Audio. Schluessel ist ein Hash,
        # nicht der Text selbst: der Cache liegt im Speicher und soll keine
        # vorgelesenen Vertragsinhalte im Klartext vorhalten.
        _CACHE_MAX_ENTRIES = int(os.getenv("TTS_CACHE_MAX_ENTRIES", "64"))
        _CACHE_MAX_BYTES = int(float(os.getenv("TTS_CACHE_MAX_MB", "256")) * 1024 * 1024)
        _tts_cache = OrderedDict()
        _tts_cache_bytes = 0
        _tts_cache_hits = 0


        def _cache_key(text: str, voice: str | None, fmt: str) -> str:
            # Feldweise gehasht statt zu einem String verkettet: so kann kein
            # Textinhalt die Feldgrenzen verschieben.
            h = hashlib.sha256()
            for teil in (text, (voice or "").strip().lower(), fmt, QWEN_CLONE_REF_WAV,
                         QWEN_CLONE_REF_SPK, QWEN_SEED, QWEN_TEMP):
                h.update(str(teil).encode("utf-8"))
                h.update(b"|")
            return h.hexdigest()


        def _cache_get(key: str):
            global _tts_cache_hits
            hit = _tts_cache.get(key)
            if hit is None:
                return None
            _tts_cache.move_to_end(key)
            _tts_cache_hits += 1
            return hit


        def _cache_put(key: str, content: bytes, media_type: str) -> None:
            global _tts_cache_bytes
            size = len(content)
            if _CACHE_MAX_ENTRIES <= 0 or size > _CACHE_MAX_BYTES:
                return  # Cache aus, oder ein einzelner Eintrag sprengt das Budget.
            if key in _tts_cache:
                _tts_cache_bytes -= len(_tts_cache[key][0])
            _tts_cache[key] = (content, media_type)
            _tts_cache.move_to_end(key)
            _tts_cache_bytes += size
            while _tts_cache and (len(_tts_cache) > _CACHE_MAX_ENTRIES
                                  or _tts_cache_bytes > _CACHE_MAX_BYTES):
                _, (alt, _mt) = _tts_cache.popitem(last=False)
                _tts_cache_bytes -= len(alt)


        @app.post("/v1/audio/speech")
        async def speech(req: SpeechRequest):
            global _tts_requests_total
            text = _strip_for_tts(req.input or "")
            if not text:
                raise HTTPException(status_code=400, detail="Leerer Input nach Bereinigung.")
            if req.max_chars and req.max_chars > 0 and len(text) > req.max_chars:
                # An Satzgrenze kuerzen, solange mehr als die Haelfte erhalten bleibt.
                truncated = text[:req.max_chars]
                boundary = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
                if boundary > req.max_chars // 2:
                    text = text[:boundary + 1]
                else:
                    text = truncated.rstrip() + "..."
            if len(text) > MAX_INPUT_CHARS:
                text = text[:MAX_INPUT_CHARS]
            # Unbekannte Formate fallen auf mp3 zurueck statt 400. ogg = Telegram.
            fmt = (req.response_format or "mp3").lower()
            key = _cache_key(text, req.voice, fmt)
            cached = _cache_get(key)
            if cached is not None:
                # Bewusst VOR dem Lock: ein Treffer wartet nicht auf eine laufende
                # Synthese. Und bewusst ohne Metrik-Eintrag -- eine Auslieferung in
                # Millisekunden wuerde den RTF-Schnitt verfaelschen.
                return Response(content=cached[0], media_type=cached[1])
            t0 = time.perf_counter()
            async with _synth_lock:
                # Zweite Pruefung im Lock: warteten zwei Anfragen auf denselben
                # Text, hat die erste ihn inzwischen erzeugt.
                cached = _cache_get(key)
                if cached is not None:
                    return Response(content=cached[0], media_type=cached[1])
                try:
                    audio = await _synth_server(text, req.voice)
                    if fmt == "wav":
                        res_content = _to_wav(audio)
                        media_type = "audio/wav"
                    elif fmt == "ogg":
                        res_content = await asyncio.to_thread(_audio_to_ogg, audio)
                        media_type = "audio/ogg"
                    else:
                        res_content = await asyncio.to_thread(_audio_to_mp3, audio)
                        media_type = "audio/mpeg"

                    t1 = time.perf_counter()
                    latency_s = t1 - t0
                    chars = len(text)
                    audio_duration_s = _get_audio_duration(audio)
                    rtf = latency_s / audio_duration_s if audio_duration_s > 0 else 0.0
                    _tts_requests_total += 1
                    _tts_requests.append({
                        "ts": time.time(),
                        "latency_s": latency_s,
                        "chars": chars,
                        "chars_per_sec": chars / latency_s if latency_s > 0 else 0,
                        "audio_duration_s": audio_duration_s,
                        "rtf": rtf
                    })
                    _cache_put(key, res_content, media_type)
                    return Response(content=res_content, media_type=media_type)
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"TTS-Fehler: {e}")


        @app.get("/v1/tts/metrics")
        async def tts_metrics():
            reqs = list(_tts_requests)
            avg_latency = sum(r["latency_s"] for r in reqs) / len(reqs) if reqs else 0.0
            avg_chars_per_sec = sum(r["chars_per_sec"] for r in reqs) / len(reqs) if reqs else 0.0
            avg_rtf = sum(r["rtf"] for r in reqs) / len(reqs) if reqs else 0.0
            return {
                "tts_requests_total": _tts_requests_total,
                "cache_hits": _tts_cache_hits,
                "cache_entries": len(_tts_cache),
                "cache_mb": round(_tts_cache_bytes / 1024 / 1024, 1),
                "avg_latency_s": round(avg_latency, 3),
                "avg_chars_per_sec": round(avg_chars_per_sec, 1),
                "avg_rtf": round(avg_rtf, 4),
                "recent": reqs[-10:],
                "mode": "clone" if CLONE_MODE else "server",
                "voice": "clone" if CLONE_MODE else (QWEN_VOICE or QWEN_DEFAULT_VOICE),
                "lang": QWEN_LANG,
            }


        @app.get("/healthz")
        async def health():
            alive = _server_proc is not None and _server_proc.poll() is None
            if not alive:
                raise HTTPException(status_code=503, detail="tts-server laeuft nicht.")
            mode = "clone" if CLONE_MODE else "server"
            voice = "clone" if CLONE_MODE else (QWEN_VOICE or QWEN_DEFAULT_VOICE)
            return {"status": "ok", "mode": mode, "voice": voice, "lang": QWEN_LANG}
    '''), "tts-service app", do_dedent=True)


    # ---------------------------------------------------------------------------
    # stt-service (faster-whisper large-v3-turbo, INT8 CPU)
    # ---------------------------------------------------------------------------
    Path("stt_service").mkdir(exist_ok=True)
    writefile("stt_service/Dockerfile.stt", textwrap.dedent("""\
        FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a
        WORKDIR /app
        RUN apt-get update \\
            && apt-get install -y --no-install-recommends wget ffmpeg ca-certificates \\
            && rm -rf /var/lib/apt/lists/*
        RUN pip install --no-cache-dir fastapi uvicorn python-multipart "faster-whisper>=1.1.0" requests
        COPY app.py /app/app.py
        RUN useradd -m -u 1000 sttuser && mkdir -p /home/sttuser/.cache/huggingface && chown -R sttuser:sttuser /app /home/sttuser
        USER sttuser
        ENV WHISPER_MODEL=large-v3-turbo \\
            WHISPER_COMPUTE_TYPE=int8 \\
            WHISPER_LANGUAGE=de \\
            HF_HOME=/home/sttuser/.cache/huggingface
        CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
    """), "Dockerfile stt-service")

    writefile("stt_service/app.py", textwrap.dedent('''\
        import asyncio
        import os
        import tempfile
        from pathlib import Path
        from contextlib import asynccontextmanager

        from fastapi import FastAPI, UploadFile, File, Form, HTTPException
        from pydantic import BaseModel

        MODEL_NAME = os.getenv("WHISPER_MODEL", "large-v3-turbo")
        COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "de")
        MAX_UPLOAD_MB = 25

        _model = None
        _model_lock = asyncio.Lock()

        async def _load_model():
            global _model
            if _model is not None:
                return _model
            async with _model_lock:
                if _model is not None:
                    return _model
                from faster_whisper import WhisperModel
                _model = await asyncio.to_thread(
                    WhisperModel, MODEL_NAME, device="cpu", compute_type=COMPUTE_TYPE,
                )
            return _model

        @asynccontextmanager
        async def lifespan(app):
            await _load_model()
            yield

        app = FastAPI(title="Argus STT (faster-whisper)", lifespan=lifespan)


        def _transcribe_sync(path: str, language: str) -> dict:
            segments_iter, info = _model.transcribe(
                path, language=language, beam_size=5, vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            text_parts = []
            segments_out = []
            for seg in segments_iter:
                text_parts.append(seg.text)
                segments_out.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                })
            return {
                "text": " ".join(p.strip() for p in text_parts).strip(),
                "language": info.language,
                "duration": round(info.duration, 2),
                "segments": segments_out,
            }


        @app.post("/v1/audio/transcriptions")
        async def transcribe(file: UploadFile = File(...), language: str = Form(None)):
            data = await file.read()
            size_mb = len(data) / (1024 * 1024)
            if size_mb > MAX_UPLOAD_MB:
                raise HTTPException(status_code=413, detail=f"Datei zu gross ({size_mb:.1f} MB > {MAX_UPLOAD_MB} MB).")
            if not data:
                raise HTTPException(status_code=400, detail="Leerer Upload.")
            await _load_model()
            lang = (language or DEFAULT_LANGUAGE).strip() or None
            if lang and lang.lower() in ("auto", "none"):
                lang = None
            with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "audio.ogg").suffix or ".ogg", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                result = await asyncio.to_thread(_transcribe_sync, tmp_path, lang)
                return result
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"STT-Fehler: {e}")
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

        @app.get("/healthz")
        async def health():
            if _model is None:
                return {"status": "loading", "model": MODEL_NAME}
            return {"status": "ok", "model": MODEL_NAME, "compute_type": COMPUTE_TYPE}
    '''), "stt-service app", do_dedent=True)


    # ---------------------------------------------------------------------------
    # crypto_utils.py
    # ---------------------------------------------------------------------------
    CRYPTO_UTILS = textwrap.dedent("""\
        import os
        from pathlib import Path
        from cryptography.fernet import Fernet

        _fernet_instance = None

        def _get_fernet() -> Fernet:
            global _fernet_instance
            if _fernet_instance is None:
                env_key = os.getenv("FERNET_KEY")
                if env_key:
                    _fernet_instance = Fernet(env_key.encode())
                else:
                    key_path = Path("/app/fernet_key.txt")
                    if not key_path.exists():
                        key_path = Path("API_Tokens/fernet_key.txt")
                    if not key_path.exists():
                        raise RuntimeError("fernet_key.txt nicht gefunden.")
                    _fernet_instance = Fernet(key_path.read_bytes().strip())
            return _fernet_instance

        def encrypt_msg(text: str) -> bytes:
            if not text:
                return b""
            return _get_fernet().encrypt(text.encode("utf-8"))

        def decrypt_msg(data: bytes) -> str:
            if not data:
                return ""
            return _get_fernet().decrypt(data).decode("utf-8")

        def load_secret(var_name: str) -> str:
            val = os.getenv(var_name, "")
            if not val:
                # 1. Versuche aus /run/secrets/ zu lesen
                secret_path = Path(f"/run/secrets/{var_name.lower()}")
                if not secret_path.exists():
                    # Fallback auf lokale Pfade
                    file_map = {
                        "POSTGRES_PASSWORD": "postgres_password.txt",
                        "HF_TOKEN": "HF_TOKEN.txt",
                        "JWT_SECRET": "jwt_secret.txt",
                        "WEBUI_SECRET_KEY": "webui_secret_key.txt",
                        "SGLANG_API_KEY": "sglang_api_key.txt",
                        "AUDIT_HMAC_KEY": "audit_hmac_key.txt",
                        "TELEGRAM_BOT_TOKEN": "telegram.txt",
                        "TELEGRAM_CHAT_ID": "telegram.txt",
                        "LANGCHAIN_API_KEY": "LANGSMITH_API_KEY.txt",
                        "SERVICE_TOKEN": "service_token.txt",
                        "GEMINI_API_KEY": "Gemini.txt",
                    }
                    filename = file_map.get(var_name)
                    if filename:
                        # Erst /run/secrets/<stem>, dann lokaler API_Tokens-Pfad.
                        candidates = [
                            Path("/run/secrets") / Path(filename).stem,
                            Path("API_Tokens") / filename,
                        ]
                        secret_path = next((p for p in candidates if p.exists()), secret_path)

                if secret_path.exists():
                    val = secret_path.read_text(encoding="utf-8").strip()
                    # telegram.txt bzw. /run/secrets/telegram ist eine KEY=VALUE-Datei
                    if secret_path.name.startswith("telegram"):
                        matched = ""
                        for line in val.splitlines():
                            if "=" in line:
                                k, _, v = line.partition("=")
                                if k.strip() == var_name:
                                    matched = v.strip()
                                    break
                        val = matched

            if val and val.startswith("ENC:"):
                try:
                    return _get_fernet().decrypt(val[4:].encode()).decode()
                except Exception as exc:
                    raise RuntimeError(f"Secret {var_name} kann nicht entschluesselt werden.") from exc
            return val
    """)
    writefile("rag_backend/crypto_utils.py", CRYPTO_UTILS, "crypto utils")


    # ---------------------------------------------------------------------------
    # models.py
    # ---------------------------------------------------------------------------
    writefile("rag_backend/models.py", textwrap.dedent("""\
        from datetime import datetime, timezone
        from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, LargeBinary, BigInteger, Boolean, Float
        from sqlalchemy.orm import DeclarativeBase, relationship

        class Base(DeclarativeBase):
            pass


        class User(Base):
            __tablename__ = "users"
            id = Column(Integer, primary_key=True)
            email = Column(String, unique=True, index=True)
            hashed_password = Column(String)
            #--- Wer schreibende Host-Aktionen freigeben darf. Kommt ueber
            #--- ensure_user_role_column statt ueber eine Alembic-Migration.
            role = Column(String, nullable=False, server_default="admin", default="admin")
            sessions = relationship("ChatSession", back_populates="user")


        class ChatSession(Base):
            __tablename__ = "chat_sessions"
            id = Column(Integer, primary_key=True)
            user_id = Column(Integer, ForeignKey("users.id"))
            created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
            messages = relationship("Message", back_populates="session")
            user = relationship("User", back_populates="sessions")


        class Message(Base):
            __tablename__ = "messages"
            id = Column(Integer, primary_key=True)
            session_id = Column(Integer, ForeignKey("chat_sessions.id"))
            sender = Column(String)
            content_encrypted = Column(LargeBinary)
            timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
            latency_ms = Column(Integer, nullable=True)
            ttft_ms = Column(Integer, nullable=True)
            tokens_per_sec = Column(Float, nullable=True)
            #--- Werkzeugzeilen, Denktext und Quellen als verschluesseltes JSON.
            #    Verschluesselt wie der Inhalt: der Denktext ist oft persoenlicher
            #    als die Antwort, und in den Werkzeugzeilen stehen Suchanfragen.
            steps_encrypted = Column(LargeBinary, nullable=True)
            session = relationship("ChatSession", back_populates="messages")


        class IngestedDocument(Base):
            __tablename__ = "ingested_documents"
            id = Column(Integer, primary_key=True)
            filename = Column(String, unique=True, index=True)
            file_path = Column(String)
            file_hash = Column(String)
            chunk_count = Column(Integer)
            ingested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


        class AuditLog(Base):
            __tablename__ = "audit_log"
            id = Column(Integer, primary_key=True)
            ts = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
            user_id = Column(Integer, nullable=True)
            tool = Column(String, nullable=False, index=True)
            tool_class = Column(String, nullable=False)
            input_hash = Column(String, nullable=False)
            output_hash = Column(String, nullable=True)
            status = Column(String, nullable=False)
            latency_ms = Column(Integer, nullable=True)
            details = Column(String, nullable=True)
            hmac_signature = Column(String, nullable=False)


        class TelegramChatSettings(Base):
            __tablename__ = "telegram_chat_settings"
            chat_id = Column(BigInteger, primary_key=True)
            voice_enabled = Column(Boolean, nullable=False, default=False)
            updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                onupdate=lambda: datetime.now(timezone.utc))


        class ReflectionState(Base):
            # Wasserzeichen fuer die Post-Session-Reflektion: bis zu welchem
            # Timestamp wurde bereits reflektiert.
            __tablename__ = "reflection_state"
            user_id = Column(Integer, primary_key=True)
            reflected_until = Column(DateTime, nullable=True)
            updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                onupdate=lambda: datetime.now(timezone.utc))


        class Mission(Base):
            # Cloud-Agenten-Mission (Setup4). Payload-Felder sind verschluesselt;
            # die PII-Platzhalter-Map verlaesst den Host nie.
            __tablename__ = "missions"
            id = Column(Integer, primary_key=True)
            user_id = Column(Integer, nullable=True)
            goal_encrypted = Column(LargeBinary, nullable=False)
            context_encrypted = Column(LargeBinary, nullable=True)
            pii_map_encrypted = Column(LargeBinary, nullable=True)
            provider = Column(String, nullable=False, default="gemini")
            status = Column(String, nullable=False, default="queued", index=True)
            current_step = Column(String, nullable=True)
            calls_used = Column(Integer, nullable=False, default=0)
            tokens_in = Column(Integer, nullable=False, default=0)
            tokens_out = Column(Integer, nullable=False, default=0)
            result_encrypted = Column(LargeBinary, nullable=True)
            error = Column(String, nullable=True)
            # Herkunft fuer "Ergebnis zurueck zur Quelle": telegram|webui|dashboard.
            origin = Column(String, nullable=False, default="telegram")
            owui_chat_id = Column(String, nullable=True)
            owui_message_id = Column(String, nullable=True)
            owui_user_id = Column(String, nullable=True)
            dashboard_session_id = Column(Integer, nullable=True)
            created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
            updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                onupdate=lambda: datetime.now(timezone.utc))
    """), "models")


    # ---------------------------------------------------------------------------
    # database.py
    # ---------------------------------------------------------------------------
    writefile("rag_backend/database.py", textwrap.dedent("""\
        import os
        import sys
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from rag_backend.crypto_utils import load_secret

        db_user = os.getenv("POSTGRES_USER", "argus_user")
        db_name = os.getenv("POSTGRES_DB", "ragdb")
        db_pass = load_secret("POSTGRES_PASSWORD")
        if db_user and db_pass:
            SQLALCHEMY_DATABASE_URL = f"postgresql://{db_user}:{db_pass}@postgres:5432/{db_name}"
        else:
            SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

        if not SQLALCHEMY_DATABASE_URL:
            print("FEHLER: DATABASE_URL konnte nicht konstruiert werden.", file=sys.stderr)
            sys.exit(1)
        engine = create_engine(SQLALCHEMY_DATABASE_URL)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


        def get_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()
    """), "database")


    # ---------------------------------------------------------------------------
    # schema_utils.py
    # ---------------------------------------------------------------------------
    writefile("rag_backend/schema_utils.py", textwrap.dedent("""\
        from sqlalchemy import inspect, text


        MESSAGE_PERFORMANCE_COLUMNS = {
            "latency_ms": "INTEGER",
            "ttft_ms": "INTEGER",
            "tokens_per_sec": "FLOAT",
            # Werkzeugschritte. BYTEA/BLOB je nach Datenbank -- der Typ wird beim
            # Anlegen aus dem Dialekt bestimmt, siehe unten.
        }
        MESSAGE_BLOB_COLUMNS = ("steps_encrypted",)


        def ensure_message_performance_columns(bind) -> None:
            inspector = inspect(bind)
            if "messages" not in inspector.get_table_names():
                return

            existing = {column["name"] for column in inspector.get_columns("messages")}
            missing = [
                (name, column_type)
                for name, column_type in MESSAGE_PERFORMANCE_COLUMNS.items()
                if name not in existing
            ]
            with bind.begin() as conn:
                for name, column_type in missing:
                    conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {column_type}"))

            # Binaerspalten getrennt: der Typname unterscheidet sich je Datenbank
            # (PostgreSQL BYTEA, SQLite BLOB), ein fester Name schluege auf einer
            # der beiden fehl.
            blob = "BYTEA" if bind.dialect.name == "postgresql" else "BLOB"
            with bind.begin() as conn:
                for name in MESSAGE_BLOB_COLUMNS:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {blob}"))


        def ensure_user_role_column(bind) -> None:
            '''users.role additiv nachziehen (Freigabe-Berechtigung).

            Bewusst hier statt als Alembic-Migration: die Revisionen 0007-0009 gehoeren
            zur Cloud-Schicht (Setup4). Eine Setup1-0010 muesste auf 0009 aufsetzen und
            waere fuer jeden Aufbau OHNE Setup4 eine zerrissene Kette. Idempotent, also
            gefahrlos bei jedem Start.'''
            inspector = inspect(bind)
            if "users" not in inspector.get_table_names():
                return
            existing = {column["name"] for column in inspector.get_columns("users")}
            if "role" in existing:
                return
            with bind.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'admin'"))
    """), "schema utils")


    # ---------------------------------------------------------------------------
    # alembic
    # ---------------------------------------------------------------------------
    Path("alembic/versions").mkdir(parents=True, exist_ok=True)

    writefile("alembic.ini", textwrap.dedent("""\
        [alembic]
        script_location = /app/alembic
        prepend_sys_path = /app

        [loggers]
        keys = root,sqlalchemy,alembic

        [handlers]
        keys = console

        [formatters]
        keys = generic

        [logger_root]
        level = WARN
        handlers = console
        qualname =

        [logger_sqlalchemy]
        level = WARN
        handlers =
        qualname = sqlalchemy.engine

        [logger_alembic]
        level = INFO
        handlers =
        qualname = alembic

        [handler_console]
        class = StreamHandler
        args = (sys.stderr,)
        level = NOTSET
        formatter = generic

        [formatter_generic]
        format = %(levelname)-5.5s [%(name)s] %(message)s
        datefmt = %%H:%%M:%%S
    """), "alembic ini")

    writefile("alembic/env.py", textwrap.dedent("""\
        import os
        import sys
        from pathlib import Path
        from alembic import context

        sys.path.insert(0, str(Path(__file__).parent.parent))

        from rag_backend.database import engine
        from rag_backend.models import Base

        target_metadata = Base.metadata


        def run_migrations_offline():
            context.configure(
                url=os.getenv("DATABASE_URL"),
                target_metadata=target_metadata,
                literal_binds=True,
            )
            with context.begin_transaction():
                context.run_migrations()


        def run_migrations_online():
            with engine.connect() as connection:
                context.configure(connection=connection, target_metadata=target_metadata)
                with context.begin_transaction():
                    context.run_migrations()


        if context.is_offline_mode():
            run_migrations_offline()
        else:
            run_migrations_online()
    """), "alembic env")

    writefile("alembic/versions/0001_initial.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0001'
        down_revision = None
        branch_labels = None
        depends_on = None


        def upgrade():
            op.create_table(
                'users',
                sa.Column('id', sa.Integer(), nullable=False),
                sa.Column('email', sa.String(), nullable=True),
                sa.Column('hashed_password', sa.String(), nullable=True),
                sa.PrimaryKeyConstraint('id'),
            )
            op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

            op.create_table(
                'chat_sessions',
                sa.Column('id', sa.Integer(), nullable=False),
                sa.Column('user_id', sa.Integer(), nullable=True),
                sa.Column('created_at', sa.DateTime(), nullable=True),
                sa.ForeignKeyConstraint(['user_id'], ['users.id']),
                sa.PrimaryKeyConstraint('id'),
            )

            op.create_table(
                'messages',
                sa.Column('id', sa.Integer(), nullable=False),
                sa.Column('session_id', sa.Integer(), nullable=True),
                sa.Column('sender', sa.String(), nullable=True),
                sa.Column('content_encrypted', sa.LargeBinary(), nullable=True),
                sa.Column('timestamp', sa.DateTime(), nullable=True),
                sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id']),
                sa.PrimaryKeyConstraint('id'),
            )


        def downgrade():
            op.drop_table('messages')
            op.drop_table('chat_sessions')
            op.drop_index(op.f('ix_users_email'), table_name='users')
            op.drop_table('users')
    """), "alembic initial migration")

    writefile("alembic/versions/0002_ingested_documents.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0002'
        down_revision = '0001'
        branch_labels = None
        depends_on = None


        def upgrade():
            op.create_table(
                'ingested_documents',
                sa.Column('id', sa.Integer(), nullable=False),
                sa.Column('filename', sa.String(), nullable=True),
                sa.Column('file_path', sa.String(), nullable=True),
                sa.Column('file_hash', sa.String(), nullable=True),
                sa.Column('chunk_count', sa.Integer(), nullable=True),
                sa.Column('ingested_at', sa.DateTime(), nullable=True),
                sa.PrimaryKeyConstraint('id'),
            )
            op.create_index(op.f('ix_ingested_documents_filename'), 'ingested_documents', ['filename'], unique=True)


        def downgrade():
            op.drop_index(op.f('ix_ingested_documents_filename'), table_name='ingested_documents')
            op.drop_table('ingested_documents')
    """), "alembic migration 0002")

    writefile("alembic/versions/0003_audit_log.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0003'
        down_revision = '0002'
        branch_labels = None
        depends_on = None


        def upgrade():
            op.create_table(
                'audit_log',
                sa.Column('id', sa.Integer(), nullable=False),
                sa.Column('ts', sa.DateTime(), nullable=True),
                sa.Column('user_id', sa.Integer(), nullable=True),
                sa.Column('tool', sa.String(), nullable=False),
                sa.Column('tool_class', sa.String(), nullable=False),
                sa.Column('input_hash', sa.String(), nullable=False),
                sa.Column('output_hash', sa.String(), nullable=True),
                sa.Column('status', sa.String(), nullable=False),
                sa.Column('latency_ms', sa.Integer(), nullable=True),
                sa.Column('hmac_signature', sa.String(), nullable=False),
                sa.PrimaryKeyConstraint('id'),
            )
            op.create_index(op.f('ix_audit_log_ts'), 'audit_log', ['ts'])
            op.create_index(op.f('ix_audit_log_tool'), 'audit_log', ['tool'])


        def downgrade():
            op.drop_index(op.f('ix_audit_log_tool'), table_name='audit_log')
            op.drop_index(op.f('ix_audit_log_ts'), table_name='audit_log')
            op.drop_table('audit_log')
    """), "alembic migration 0003")

    writefile("alembic/versions/0004_telegram_chat_settings.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0004'
        down_revision = '0003'
        branch_labels = None
        depends_on = None


        def upgrade():
            op.create_table(
                'telegram_chat_settings',
                sa.Column('chat_id', sa.BigInteger(), nullable=False),
                sa.Column('voice_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
                sa.Column('updated_at', sa.DateTime(), nullable=True),
                sa.PrimaryKeyConstraint('chat_id'),
            )


        def downgrade():
            op.drop_table('telegram_chat_settings')
    """), "alembic migration 0004")


    writefile("alembic/versions/0005_reflection_state.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0005'
        down_revision = '0004'
        branch_labels = None
        depends_on = None


        def upgrade():
            op.create_table(
                'reflection_state',
                sa.Column('user_id', sa.Integer(), nullable=False),
                sa.Column('reflected_until', sa.DateTime(), nullable=True),
                sa.Column('updated_at', sa.DateTime(), nullable=True),
                sa.PrimaryKeyConstraint('user_id'),
            )


        def downgrade():
            op.drop_table('reflection_state')
    """), "alembic migration 0005")

    writefile("alembic/versions/0006_add_details_to_audit_log.py", textwrap.dedent("""\
        from alembic import op
        import sqlalchemy as sa

        revision = '0006'
        down_revision = '0005'
        branch_labels = None
        depends_on = None


        def upgrade():
            op.add_column('audit_log', sa.Column('details', sa.String(), nullable=True))


        def downgrade():
            op.drop_column('audit_log', 'details')
    """), "alembic migration 0006")
    # ---------------------------------------------------------------------------
    # utils.py
    # ---------------------------------------------------------------------------
    writefile("rag_backend/utils.py", textwrap.dedent("""\
        import json
        import re
        from datetime import datetime
        from zoneinfo import ZoneInfo

        WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                  "Juli", "August", "September", "Oktober", "November", "Dezember"]

        # 1 Token ~ 2.5 Zeichen (deutscher Text). Einheitliche Schaetzung fuer
        # Kontext-Budget und Kompaktierungs-Schwelle.
        CHARS_PER_TOKEN = 2.5


        def get_datetime_berlin() -> str:
            # Feste Zeitzone Europe/Berlin: der Container laeuft in UTC.
            try:
                now = datetime.now(ZoneInfo("Europe/Berlin"))
            except Exception:
                now = datetime.now().astimezone()
            weekday = WEEKDAYS[now.weekday()]
            month = MONTHS[now.month - 1]
            return f"{weekday}, der {now.day}. {month} {now.year}, {now.strftime('%H:%M')} Uhr"


        # ---- Gemeinsame LLM-Helper (genutzt von agent.py, memory.py, missions.py,
        #      redactor.py, telegram_bot.py).

        def strip_think(text) -> str:
            # Entfernt <think>-Bloecke aus Qwen-Antworten, auch bei abgeschnittenem
            # Block oder haengendem Closer ohne Opener.
            if not isinstance(text, str):
                text = "" if text is None else str(text)
            out = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.DOTALL)
            if "</think>" in out:
                out = out.rsplit("</think>", 1)[-1]
            return out.strip()


        def extract_json(text, kind: str = "object"):
            # Balance-Scanner statt greedy Regex: liefert den ersten vollstaendig
            # parsebaren JSON-Block. None wenn keiner da ist -- den Fallback
            # entscheidet der Aufrufer.
            if not isinstance(text, str) or not text:
                return None
            open_ch, close_ch = ("[", "]") if kind == "array" else ("{", "}")
            start = text.find(open_ch)
            while start != -1:
                depth, in_string, escape = 0, False, False
                for i in range(start, len(text)):
                    ch = text[i]
                    if escape:
                        escape = False
                        continue
                    if ch == "\\\\" and in_string:
                        escape = True
                        continue
                    if ch == '"':
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch == open_ch:
                        depth += 1
                    elif ch == close_ch:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[start:i + 1])
                            except Exception:
                                break
                start = text.find(open_ch, start + 1)
            return None


        def make_qwen_llm(*, temperature: float, top_p: float, max_tokens: int,
                          thinking: bool = False, presence_penalty: float = 0.0,
                          min_p: float | None = None, api_key: str | None = None):
            # Fabrik fuer alle lokalen SGLang/Qwen-Instanzen. Imports lokal, damit
            # utils ohne langchain importierbar bleibt.
            import os
            from langchain_openai import ChatOpenAI
            extra_body = {"chat_template_kwargs": {"enable_thinking": thinking}, "top_k": 20}
            if min_p is not None:
                extra_body["min_p"] = min_p
            return ChatOpenAI(
                model=os.getenv("OAI_MODEL"),
                base_url=os.getenv("OAI_BASE_URL"),
                api_key=api_key or os.getenv("SGLANG_API_KEY", "dummy"),
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
    """), "utils")


    # ---------------------------------------------------------------------------
    # main.py
    # ---------------------------------------------------------------------------
    MAIN_PY = r"""import asyncio
import logging
import os
import json
import secrets
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from jwt import InvalidTokenError
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from langchain_core.messages import AIMessage, HumanMessage
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from rag_backend.models import User, ChatSession, Message, AuditLog, IngestedDocument, Mission, Base
from rag_backend.database import engine, get_db, SessionLocal
from rag_backend.agent import agent_executor, startup_load_models, startup_mcp_and_graph, _TOOL_STATUS
from rag_backend.memory import run_reflection_cycle
from rag_backend.crypto_utils import encrypt_msg, decrypt_msg, load_secret
from rag_backend import metrics
from rag_backend.schema_utils import ensure_message_performance_columns, ensure_user_role_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# httpx loggt auf INFO jede Request-URL -- bei python-telegram-bot steht darin
# der BOT-TOKEN im Klartext. Auf WARNING heben verhindert das Leck.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


#--- Zweite Linie hinter dem Level-Anheben: das greift nur, solange NIEMAND den
#--- Token auf WARNING oder hoeher schreibt. Eine python-telegram-bot-Exception
#--- traegt die aufgerufene URL im Text, und eine Bibliothek im Debug-Modus
#--- reicht ebenfalls. Der Filter haengt an den ROOT-HANDLERN und sieht damit
#--- jeden Datensatz, auch die aus fremden Loggern propagierten.
class _SecretRedactingFilter(logging.Filter):
    #--- Zweiter Anker fuer den Fall, dass load_secret nichts liefert: in der
    #--- Telegram-API-URL steht der Token als '/bot<id>:<secret>/'.
    _URL_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
    _MASK = "[REDACTED_TELEGRAM_TOKEN]"

    def __init__(self, secrets: list):
        super().__init__()
        #--- Ein leerer oder sehr kurzer Wert ist kein Secret, sondern ein
        #--- Suchmuster, das die halben Logs schwaerzen wuerde.
        self._literals = [s for s in secrets if s and len(s) >= 12]

    def _redact(self, text: str) -> str:
        for secret in self._literals:
            text = text.replace(secret, self._MASK)
        return self._URL_TOKEN_RE.sub("bot" + self._MASK, text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = self._redact(message)
            if redacted != message:
                record.msg, record.args = redacted, ()
            #--- Tracebacks laufen an getMessage() vorbei. Vorformatieren und
            #--- ebenfalls saeubern -- der Formatter nimmt danach exc_text.
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = self._redact(record.exc_text)
        except Exception:
            pass  # Logging darf die Anwendung nie stoppen.
        return True


def _install_secret_redaction() -> None:
    try:
        _secrets = [load_secret("TELEGRAM_BOT_TOKEN")]
    except Exception:
        _secrets = []
    _redactor = _SecretRedactingFilter(_secrets)
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_redactor)


_install_secret_redaction()


_ph = PasswordHasher()
JWT_SECRET = load_secret("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", 1))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Tool-Statuszeilen kommen als eigenes "status"-Feld, nicht ueber eine
# Emoji-Praefix-Erkennung -- die wuerde echte Antworten loeschen.

# ---- Schemas, Token- & Startup-Helpers #---

class RegisterRequest(BaseModel):
    email: str
    password: str

def extract_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "file":
                file_data = block.get("file", {})
                parts.append(file_data.get("content", ""))
        return "\n\n".join(p for p in parts if p)
    return str(content) if content else ""

# Bild-Bloecke aus einer OpenAI-Message einsammeln. Caps: Anzahl pro Anfrage
# und Groesse pro data-URI.
_MAX_IMAGES = int(os.getenv("MAX_IMAGES_PER_REQUEST", 2))
_MAX_IMAGE_DATA_CHARS = int(os.getenv("MAX_IMAGE_DATA_CHARS", 10 * 1024 * 1024))

def extract_image_urls(content) -> list:
    urls = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            u = block.get("image_url")
            u = u.get("url", "") if isinstance(u, dict) else str(u or "")
            if not u or len(u) > _MAX_IMAGE_DATA_CHARS:
                continue
            urls.append(u)
            if len(urls) >= _MAX_IMAGES:
                break
    return urls

def create_token(uid: int) -> str:
    payload = {
        "sub": str(uid),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_service_user_if_not_exists(db: Session):
    from sqlalchemy import text as _sa_text
    if not db.get(User, 0):
        db.add(User(
            id=0,
            email="service-account@system.local",
            hashed_password="__service_account_no_login__",
            # Der Argus-Chat laeuft unter diesem Konto und muss Host-Aktionen
            # freigeben duerfen.
            role="admin",
        ))
        db.commit()
        try:
            db.execute(_sa_text(
                "SELECT setval(pg_get_serial_sequence('users', 'id'), GREATEST(1, (SELECT MAX(id) FROM users)))"
            ))
            db.commit()
        except Exception:
            pass

def _run_migrations():
    if os.getenv("SKIP_MIGRATIONS", "false").lower() == "true":
        return
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    from sqlalchemy import inspect as sa_inspect
    from rag_backend.database import engine

    cfg = AlembicConfig("/app/alembic.ini")

    with engine.connect() as conn:
        insp = sa_inspect(conn)
        table_names = insp.get_table_names()
        has_alembic = "alembic_version" in table_names
        has_users = "users" in table_names

    if has_users and not has_alembic:
        log.info("Bestehende DB ohne Alembic-History erkannt -- stemple auf Version 0001.")
        alembic_command.stamp(cfg, "0001")

    alembic_command.upgrade(cfg, "head")

    # Neue Modelle ohne eigene Alembic-Migration additiv anlegen. create_all
    # legt nur fehlende Tabellen an.
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as _e:
        log.warning(f"create_all (Zusatztabellen) fehlgeschlagen: {_e}")

    try:
        ensure_message_performance_columns(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for messages performance columns failed: {_e}")

    try:
        ensure_user_role_column(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for users.role failed: {_e}")

def _ensure_qdrant_collection():
    import qdrant_client
    # Erstellungslogik lebt zentral in ingest.py. dim kommt hier aus der .env,
    # weil das Embedding-Modell beim Startup noch nicht geladen sein soll.
    from rag_backend.ingest import ensure_qdrant_collection
    url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection = os.getenv("QDRANT_COLLECTION", "docs")
    dim = int(os.getenv("EMBEDDING_DIM", 1024))
    try:
        qc = qdrant_client.QdrantClient(url=url)
        created = ensure_qdrant_collection(qc, collection, dim)
        if created:
            log.info(f"Qdrant Collection '{collection}' erstellt (dim={dim}).")
        else:
            log.info(f"Qdrant Collection '{collection}' vorhanden.")
    except Exception as e:
        log.warning(f"Qdrant Collection konnte nicht erstellt werden: {e}")

async def _reflection_idle_watcher(app: FastAPI):
    # Post-Session-Reflektion: feuert nach REFLECTION_IDLE_SECONDS Leerlauf,
    # genau einmal pro Leerlaufphase.
    idle_threshold = int(os.getenv("REFLECTION_IDLE_SECONDS", "300"))
    check_interval = int(os.getenv("REFLECTION_CHECK_INTERVAL", "120"))
    last_marker = None
    while True:
        try:
            await asyncio.sleep(check_interval)
            last = getattr(app.state, "last_activity", None)
            if last is None or last == last_marker:
                continue
            idle = (datetime.now(timezone.utc) - last).total_seconds()
            if idle < idle_threshold:
                continue
            log.info(f"Leerlauf {int(idle)}s -> starte Post-Session-Reflektion.")
            await run_reflection_cycle()
            last_marker = last
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Reflection-Watcher: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.last_activity = None
    await asyncio.to_thread(_run_migrations)
    db = SessionLocal()
    try:
        create_service_user_if_not_exists(db)
        metrics.load_historical_metrics(db)
    finally:
        db.close()
    await asyncio.to_thread(_ensure_qdrant_collection)

    #--- MCP + Graph synchron vor yield, damit erste Requests ein komplettes
    #    Tool-Set sehen.
    app.state.mcp_started = False
    try:
        await startup_mcp_and_graph()
        app.state.mcp_started = True
    except Exception as e:
        log.warning(f"startup_mcp_and_graph fehlgeschlagen: {e}")

    #--- Embedding/Reranker/HTTP-Clients im Background.
    app.state.loader_task = asyncio.create_task(startup_load_models())

    app.state.telegram_started = False
    if (os.getenv("ACTION_ENGINE_ENABLED", "false").lower() == "true"
            and load_secret("TELEGRAM_BOT_TOKEN")):
        from rag_backend.telegram_bot import start_bot
        app.state.telegram_task = asyncio.create_task(start_bot())
        app.state.telegram_started = True

    app.state.reflection_task = asyncio.create_task(_reflection_idle_watcher(app))

    #--- Cloud-Agenten (Setup4): Missions-Loop. Import geschuetzt, damit das
    #    Backend auch ohne Setup4-Output bootet.
    app.state.mission_task = None
    if os.getenv("CLOUD_AGENTS_ENABLED", "true").lower() == "true":
        try:
            from rag_backend.cloud_agents.missions import mission_loop
            app.state.mission_task = asyncio.create_task(mission_loop(app))
        except ImportError as e:
            log.warning(f"Cloud-Agents nicht verfuegbar (Setup4 nicht ausgefuehrt?): {e}")

    #--- Uploads-Ordner sauber halten (UPLOAD_RETENTION_HOURS, 0 = aus).
    app.state.uploads_task = asyncio.create_task(_uploads_sweeper())

    yield

    if getattr(app.state, "telegram_started", False):
        try:
            from rag_backend.telegram_bot import stop_bot
            await stop_bot()
        except Exception:
            pass
    if getattr(app.state, "mcp_started", False):
        try:
            from rag_backend.mcp_loader import shutdown_mcp
            await shutdown_mcp()
        except Exception:
            pass
    task = getattr(app.state, "loader_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    rtask = getattr(app.state, "reflection_task", None)
    if rtask and not rtask.done():
        rtask.cancel()
        try:
            await rtask
        except (asyncio.CancelledError, Exception):
            pass
    mtask = getattr(app.state, "mission_task", None)
    if mtask and not mtask.done():
        mtask.cancel()
        try:
            await mtask
        except (asyncio.CancelledError, Exception):
            pass
    utask = getattr(app.state, "uploads_task", None)
    if utask and not utask.done():
        utask.cancel()
        try:
            await utask
        except (asyncio.CancelledError, Exception):
            pass

def _init_tracing() -> None:
    #--- Lokales Tracing nach Phoenix. FAIL-OPEN: laeuft der Container nicht,
    #--- startet das Backend trotzdem. Observability ist Beiwerk.
    endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
    if not endpoint:
        log.info("Tracing: kein PHOENIX_COLLECTOR_ENDPOINT gesetzt -- deaktiviert.")
        return
    try:
        from phoenix.otel import register
        from openinference.instrumentation.langchain import LangChainInstrumentor
        tracer_provider = register(
            project_name=os.getenv("PHOENIX_PROJECT_NAME", "argus"),
            endpoint=endpoint.rstrip("/") + "/v1/traces",
            batch=True,               # Spans gebuendelt senden, nicht je Span ein Request
            set_global_tracer_provider=False,
            verbose=False,
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider, skip_dep_check=True)
        log.info(f"Tracing aktiv: Phoenix unter {endpoint}")
    except Exception as e:
        log.warning(f"Tracing konnte nicht gestartet werden ({type(e).__name__}: {e}) -- laeuft ohne.")


_init_tracing()

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="RAG Backend", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

#--- Host-Header-Gate gegen DNS-Rebinding. Die lesenden Dashboard-Endpoints
#--- pruefen keinen Origin; die Bindung an 127.0.0.1 allein schuetzt sie nicht.
#--- Zuletzt hinzugefuegt = aeusserste Schicht, laeuft vor CORS und allen Routen.
#--- 'rag-backend' MUSS drinbleiben (open-webui), ebenso 'localhost'
#--- (Healthcheck). Der Port ist nicht Teil des Vergleichs.
_TRUSTED_HOSTS = [
    h.strip() for h in os.getenv(
        "TRUSTED_HOSTS", "127.0.0.1,localhost,rag-backend"
    ).split(",") if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_TRUSTED_HOSTS)

#--- Sicherheits-Header. Zweite Linie hinter escapeHtml: das Dashboard zeigt
#--- Text an, den ein Modell geschrieben hat. Rutscht etwas durch, verhindert
#--- connect-src/img-src wenigstens den Abfluss.
#--- 'unsafe-inline' bei Skripten ist noetig, solange Knoepfe onclick tragen.
#--- Kein fremder Host: die Schriften liegen lokal unter static/fonts/.
_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    # Ablaufverfolgung laeuft im eigenen Container auf 6006 und wird eingebettet.
    "frame-src 'self' http://127.0.0.1:6006 http://localhost:6006; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response

# ---- Dashboard (statische Seite, same-origin unter /dashboard/) #---
class _DashboardStatics(StaticFiles):
    '''HTML immer revalidieren, versionierte Assets dauerhaft cachen.

    Setup_Dashboard haengt an jedes Skript/Stylesheet ein ?v=<inhaltshash>. Das nuetzt
    aber nichts, solange der Browser die index.html SELBST aus dem Cache nimmt: dann
    stehen dort weiter die alten Verweise. Ergebnis war ein Dashboard-Rebuild, der
    wirkungslos aussah, bis jemand Strg+F5 drueckte.
    Die HTML ist wenige KB gross -- eine Revalidierung pro Aufruf kostet nichts, waehrend
    die gehashten Assets ohne Ablauf im Cache bleiben duerfen (ein geaenderter Inhalt
    bekommt ohnehin eine neue URL).

    (Dreifach-Single-Quotes: das MAIN_PY-Template ist mit dreifach-Double-Quotes
    begrenzt und wuerde hier sonst vorzeitig enden.)'''

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif b"v=" in scope.get("query_string", b""):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


#--- Ohne diese Zeile liefert StaticFiles die Schriften als
#--- application/octet-stream aus.
import mimetypes

mimetypes.add_type("font/woff2", ".woff2")

_DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", "/app/rag_backend/static")
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/dashboard", _DashboardStatics(directory=_DASHBOARD_DIR, html=True), name="dashboard")

# ---- Output-Bereinigung, Auth & CSRF #---

def clean_agent_output(text: str | None) -> str:
    if not text:
        return ""
    
    def replace_think(match):
        think_content = match.group(1).strip()
        if not think_content:
            return ""
        return f"\n<details>\n<summary>🤔 Gedankengang</summary>\n\n{think_content}\n</details>\n\n"
        
    return re.sub(r"<think>(.*?)</think>", replace_think, text, flags=re.DOTALL).strip()

# Status-Emojis direkt aus den _TOOL_STATUS-Labels abgeleitet statt aus einer
# zweiten, driftfaehigen Liste. Dazu die Zeilen ohne _TOOL_STATUS-Eintrag.
_STATUS_EMOJIS = tuple({fn({})[0] for fn in _TOOL_STATUS.values()}) + (
    "⏳", "\U0001F9E0", "\U0001F4DD", "\U0001F527",
)


def strip_status_lines(text: str | None) -> str:
    if not text:
        return ""
    # Vergleich auf Basis-Codepoints OHNE Variation Selector (U+FE0F).
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip().replace("️", "")
        if s and any(s.startswith(emoji) for emoji in _STATUS_EMOJIS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _sse_delta(content: str | None = None, reasoning: str | None = None) -> str:
    '''OpenAI-SSE-Chunk-Envelope -- eine Quelle fuer alle Streaming-Zweige.'''
    delta = {"role": "assistant"}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    else:
        delta["content"] = content
    return "data: " + json.dumps({
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion.chunk",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": os.getenv("OAI_MODEL"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }) + "\n\n"


def _completion_json(message: dict) -> dict:
    '''OpenAI-Completion-Envelope fuer die Nicht-Streaming-Zweige.'''
    return {
        "id": f"chatcmpl-{secrets.token_hex(16)}",
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": os.getenv("OAI_MODEL"),
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        sub = payload["sub"]
        if sub == "service":
            uid = 0
        else:
            uid = int(sub)
            if uid == 0:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungültiges Token.")
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltiges oder abgelaufenes Token.")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Benutzer nicht gefunden.")
    return user

def check_csrf_headers(request: Request):
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    allowed = [o.lower() for o in _CORS_ORIGINS]
    
    if origin:
        if origin.lower() not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Origin nicht erlaubt.")
        return
        
    if referer:
        try:
            from urllib.parse import urlparse
            ref_parsed = urlparse(referer)
            ref_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}"
            if ref_origin.lower() not in allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Referer nicht erlaubt.")
        except Exception:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Ungueltiger Referer.")


#--- Dashboard-CSRF: strikt Same-Origin. Der Cookie ist SameSite=Lax -- ohne
#    dieses Gate waere jede schreibende Aktion per fremder Seite ausloesbar.
_DASHBOARD_ORIGINS = {
    o.strip().lower()
    for o in os.getenv(
        "DASHBOARD_ORIGINS",
        "http://127.0.0.1:7860,http://localhost:7860",
    ).split(",")
    if o.strip()
}


def check_dashboard_csrf(request: Request):
    from urllib.parse import urlparse

    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if referer:
            try:
                p = urlparse(referer)
                origin = f"{p.scheme}://{p.netloc}"
            except Exception:
                origin = None
    #--- Fehlender Origin UND Referer = kein Browser-Kontext. Bewusst abgelehnt.
    if not origin or origin.lower() not in _DASHBOARD_ORIGINS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="CSRF-Schutz: schreibende Dashboard-Aktionen nur same-origin.",
        )


# ---- Dashboard-Anmeldung: Konten, Rollen, Sitzungs-Cookie #---
#--- Zwei Rollen: 'admin' darf alles, 'chat' nur sprechen und schreiben. Jede
#--- Pruefung sitzt SERVERSEITIG an der Route.
DASHBOARD_ROLES = ("admin", "chat")
_DASHBOARD_COOKIE = os.getenv("DASHBOARD_COOKIE_NAME", "argus_session")
_DASHBOARD_SESSION_HOURS = int(os.getenv("DASHBOARD_SESSION_HOURS", "12"))
#--- 'Angemeldet bleiben'. Die Laufzeit steckt IM Token, nicht nur im Cookie.
_DASHBOARD_REMEMBER_DAYS = int(os.getenv("DASHBOARD_REMEMBER_DAYS", "7"))
#--- id=0 ist das Dienstkonto. Es hat kein anmeldbares Passwort und taucht in
#--- keiner Benutzerliste auf.
_SERVICE_UID = 0


class DashboardLoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False


class DashboardUserIn(BaseModel):
    username: str
    password: str
    role: str = "chat"


class DashboardPasswordIn(BaseModel):
    password: str


def _dashboard_session_token(uid: int, seconds: int) -> str:
    #--- Eigener scope-Anspruch: ein Dashboard-Cookie ist KEIN API-Token. Ohne
    #--- die Trennung liesse sich der Service-Token als Sitzungs-Cookie setzen.
    payload = {
        "sub": str(uid),
        "scope": "dashboard",
        "exp": datetime.now(timezone.utc) + timedelta(seconds=seconds),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _ist_https(request: Request) -> bool:
    #--- Hinter einem Tunnel (Cloudflare, Reverse Proxy) kommt die Anfrage
    #--- intern per HTTP an; das echte Schema steht in X-Forwarded-Proto.
    fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return fwd == "https" or request.url.scheme == "https"


def _set_session_cookie(response: Response, uid: int, remember: bool = False,
                        request: Request | None = None) -> None:
    seconds = (_DASHBOARD_REMEMBER_DAYS * 86400 if remember
               else _DASHBOARD_SESSION_HOURS * 3600)
    #--- Secure nur ueber HTTPS: fest auf True wuerde der Browser das Cookie beim
    #--- lokalen Zugang ueber http://127.0.0.1 verwerfen -- die Anmeldung liefe
    #--- dann ins Leere, ohne Fehlermeldung.
    response.set_cookie(
        _DASHBOARD_COOKIE,
        _dashboard_session_token(uid, seconds),
        max_age=seconds,
        httponly=True,
        secure=bool(request is not None and _ist_https(request)),
        samesite="lax",
        path="/",
    )


def _login_accounts(db: Session):
    return db.query(User).filter(User.id != _SERVICE_UID).order_by(User.id.asc()).all()


def dashboard_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(_DASHBOARD_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Nicht angemeldet.")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("scope") != "dashboard":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltige Sitzung.")
        uid = int(payload["sub"])
    except HTTPException:
        raise
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sitzung abgelaufen.")
    if uid == _SERVICE_UID:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltige Sitzung.")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Konto nicht mehr vorhanden.")
    return user


def dashboard_admin(user: User = Depends(dashboard_user)) -> User:
    if getattr(user, "role", "chat") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Dieser Bereich ist Administratoren vorbehalten.")
    return user


def _register_allowed() -> bool:
    #--- Fail-closed wie ENABLE_BACKEND_SIGNUP: alles ausser exakt "true" sperrt.
    return os.getenv("DASHBOARD_ALLOW_REGISTER", "true").strip().lower() == "true"


@app.get("/v1/dashboard/auth/state", tags=["Dashboard"])
def dashboard_auth_state(request: Request, db: Session = Depends(get_db)):
    #--- Erstlauf: solange kein Konto existiert, bietet die Oberflaeche das
    #--- Anlegen des ersten Administrators an.
    accounts = _login_accounts(db)
    out = {"needs_setup": not accounts, "authenticated": False,
           "allow_register": bool(accounts) and _register_allowed()}
    try:
        user = dashboard_user(request, db)
    except HTTPException:
        return out
    out.update(authenticated=True, username=user.email,
               role=getattr(user, "role", "chat"), user_id=user.id)
    return out


@app.post("/v1/dashboard/auth/register", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_REGISTER_RATE_LIMIT", "5/minute"))
def dashboard_auth_register(request: Request, body: DashboardLoginIn,
                            db: Session = Depends(get_db),
                            csrf = Depends(check_dashboard_csrf)):
    '''Selbstregistrierung. Das angelegte Konto bekommt IMMER die Rolle 'chat'.

    Die Rolle steht hier hart im Code und kommt nicht aus dem Body -- sonst koennte
    sich jeder mit einem zusaetzlichen Feld zum Administrator machen. Aus demselben
    Grund taugt der alte /register-Endpunkt nicht als Grundlage: er setzt gar keine
    Rolle, und der Spalten-Default ist "admin".'''
    if not _register_allowed():
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Selbstregistrierung ist abgeschaltet.")
    if not _login_accounts(db):
        #--- Vor dem ersten Konto fuehrt der Weg ueber /auth/setup.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Zuerst muss das Administrator-Konto angelegt werden.")
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    if db.query(User).filter_by(email=name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Benutzername ist vergeben.")
    user = User(email=name, hashed_password=_ph.hash(body.password), role="chat")
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info(f"Selbstregistrierung: '{user.email}' (Rolle chat, id={user.id})")
    resp = JSONResponse({"status": "ok", "username": user.email, "role": user.role,
                         "user_id": user.id})
    _set_session_cookie(resp, user.id, remember=body.remember, request=request)
    return resp


@app.post("/v1/dashboard/auth/setup", tags=["Dashboard"])
@limiter.limit("5/minute")
def dashboard_auth_setup(request: Request, body: DashboardUserIn,
                         db: Session = Depends(get_db),
                         csrf = Depends(check_dashboard_csrf)):
    if _login_accounts(db):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Es existiert bereits ein Konto -- bitte anmelden.")
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    user = User(email=name, hashed_password=_ph.hash(body.password), role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)
    #--- Bestehende Verlaeufe lagen unter dem Dienstkonto und gehen an den ersten
    #--- Administrator ueber, statt unerreichbar zu werden.
    db.query(ChatSession).filter_by(user_id=_SERVICE_UID).update({"user_id": user.id})
    db.commit()
    resp = JSONResponse({"status": "ok", "username": user.email, "role": user.role,
                         "user_id": user.id})
    _set_session_cookie(resp, user.id, request=request)
    return resp


@app.post("/v1/dashboard/auth/login", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_LOGIN_RATE_LIMIT", "10/minute"))
def dashboard_auth_login(request: Request, body: DashboardLoginIn,
                         db: Session = Depends(get_db),
                         csrf = Depends(check_dashboard_csrf)):
    user = db.query(User).filter_by(email=body.username.strip()).first()
    #--- Gleiche Meldung fuer 'Konto gibt es nicht' und 'Passwort falsch'.
    if not user or user.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Anmeldung fehlgeschlagen.")
    try:
        _ph.verify(user.hashed_password, body.password)
    except (VerifyMismatchError, InvalidHash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Anmeldung fehlgeschlagen.")
    if _ph.check_needs_rehash(user.hashed_password):
        user.hashed_password = _ph.hash(body.password)
        db.commit()
    resp = JSONResponse({"status": "ok", "username": user.email,
                         "role": getattr(user, "role", "chat"), "user_id": user.id,
                         "remember_days": _DASHBOARD_REMEMBER_DAYS if body.remember else 0})
    _set_session_cookie(resp, user.id, remember=body.remember, request=request)
    return resp


@app.post("/v1/dashboard/auth/logout", tags=["Dashboard"])
def dashboard_auth_logout(csrf = Depends(check_dashboard_csrf)):
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(_DASHBOARD_COOKIE, path="/")
    return resp


@app.post("/v1/dashboard/auth/password", tags=["Dashboard"])
def dashboard_auth_password(body: DashboardPasswordIn,
                            user: User = Depends(dashboard_user),
                            db: Session = Depends(get_db),
                            csrf = Depends(check_dashboard_csrf)):
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passwort ab 8 Zeichen.")
    user.hashed_password = _ph.hash(body.password)
    db.commit()
    return {"status": "ok"}


@app.get("/v1/dashboard/users", tags=["Dashboard"])
def dashboard_users(db: Session = Depends(get_db), admin: User = Depends(dashboard_admin)):
    from sqlalchemy import func

    #--- Zahlen je Konto in EINER Abfrage statt einer pro Konto.
    counts = dict(
        db.query(ChatSession.user_id, func.count(ChatSession.id))
        .group_by(ChatSession.user_id).all()
    )
    msg_rows = (db.query(ChatSession.user_id,
                         func.count(Message.id),
                         func.max(Message.timestamp))
                .join(Message, Message.session_id == ChatSession.id)
                .group_by(ChatSession.user_id).all())
    msgs = {uid: (n, last) for uid, n, last in msg_rows}

    out = []
    for u in _login_accounts(db):
        n_msgs, last = msgs.get(u.id, (0, None))
        out.append({
            "id": u.id,
            "username": u.email,
            "role": getattr(u, "role", "chat"),
            "chats": counts.get(u.id, 0),
            "messages": n_msgs,
            "last_active": last.isoformat() if last else None,
            "is_self": u.id == admin.id,
        })
    return {"users": out}


@app.post("/v1/dashboard/users", tags=["Dashboard"])
def dashboard_user_create(body: DashboardUserIn, db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin),
                          csrf = Depends(check_dashboard_csrf)):
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    if body.role not in DASHBOARD_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekannte Rolle.")
    if db.query(User).filter_by(email=name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Benutzername ist vergeben.")
    user = User(email=name, hashed_password=_ph.hash(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.email, "role": user.role, "chats": 0}


@app.post("/v1/dashboard/users/{user_id}/password", tags=["Dashboard"])
def dashboard_user_password(user_id: int, body: DashboardPasswordIn,
                            db: Session = Depends(get_db),
                            admin: User = Depends(dashboard_admin),
                            csrf = Depends(check_dashboard_csrf)):
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passwort ab 8 Zeichen.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    target.hashed_password = _ph.hash(body.password)
    db.commit()
    return {"status": "ok"}


@app.post("/v1/dashboard/users/{user_id}/role", tags=["Dashboard"])
def dashboard_user_role(user_id: int, role: str, db: Session = Depends(get_db),
                        admin: User = Depends(dashboard_admin),
                        csrf = Depends(check_dashboard_csrf)):
    if role not in DASHBOARD_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekannte Rolle.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    #--- Der letzte Administrator darf sich nicht selbst degradieren.
    if target.role == "admin" and role != "admin":
        remaining = db.query(User).filter(User.role == "admin",
                                          User.id != user_id, User.id != _SERVICE_UID).count()
        if not remaining:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="Das ist der letzte Administrator.")
    target.role = role
    db.commit()
    return {"status": "ok", "id": target.id, "role": target.role}


@app.delete("/v1/dashboard/users/{user_id}", tags=["Dashboard"])
def dashboard_user_delete(user_id: int, db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin),
                          csrf = Depends(check_dashboard_csrf)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Das eigene Konto laesst sich nicht loeschen.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    #--- Chats des Kontos gehen mit, sonst bleiben verschluesselte Verlaeufe
    #--- ohne Besitzer stehen.
    sessions = db.query(ChatSession).filter_by(user_id=user_id).all()
    for s in sessions:
        db.query(Message).filter_by(session_id=s.id).delete()
        db.delete(s)
    db.delete(target)
    db.commit()
    return {"status": "deleted", "id": user_id, "chats_removed": len(sessions)}


@app.post("/register")
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    if os.getenv("ENABLE_BACKEND_SIGNUP", "false").lower() != "true":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Registrierung deaktiviert.")
    if db.query(User).filter_by(email=body.email).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Email existiert bereits.")
    db.add(User(email=body.email, hashed_password=_ph.hash(body.password)))
    db.commit()
    return {"email": body.email}

@app.post("/login")
@limiter.limit("5/minute")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=form.username).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Falsche Anmeldedaten.")
    try:
        _ph.verify(user.hashed_password, form.password)
    except (VerifyMismatchError, InvalidHash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Falsche Anmeldedaten.")
    if _ph.check_needs_rehash(user.hashed_password):
        user.hashed_password = _ph.hash(form.password)
        db.commit()
    return {"access_token": create_token(user.id), "token_type": "bearer"}

@app.get("/v1/models", tags=["OpenAI Proxy"])
async def proxy_models():
    model_name = os.getenv("OAI_MODEL", "rag-agent-model")
    return {"data": [{"id": model_name, "object": "model"}]}

@app.get("/healthz", tags=["System"])
async def health():
    return {"status": "ok"}

# ---- Confirmation Endpoints (Action-Engine Phase 2) #---
#--- Jeder Endpoint reicht die User-ID des Aufrufers als actor durch. Nur wer
#--- die Aktion ausgeloest hat, darf sie ueber HTTP sehen oder freigeben.
#--- Der Telegram-Weg bleibt unveraendert (Chat-ID = Besitzer-Kanal).

@app.get("/v1/confirmations/pending", tags=["Action-Engine"])
async def list_pending_confirmations(current_user: User = Depends(get_current_user)):
    if os.getenv("ACTION_ENGINE_ENABLED", "false").lower() != "true":
        return {"confirmations": []}
    from rag_backend.action_engine.confirmation import confirmation_store
    pending = confirmation_store.list_pending(actor=current_user.id)
    return {"confirmations": [r.model_dump() for r in pending]}

def _require_admin(user: User) -> None:
    '''Nur Administratoren duerfen Host-Aktionen freigeben.

    Heute hat jedes Konto die Rolle "admin" (Einzelnutzer-Betrieb) -- der Check ist
    das Fundament fuer den Fall, dass weitere Nutzer dazukommen: die duerfen den
    Assistenten dann benutzen, aber keine schreibenden Aktionen auf dem Rechner
    freigeben. Lesen der eigenen offenen Anfragen bleibt erlaubt.'''
    if getattr(user, "role", "admin") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Nur Administratoren duerfen Host-Aktionen freigeben.",
        )

@app.post("/v1/confirmations/{confirmation_id}/approve", tags=["Action-Engine"])
async def approve_confirmation(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
    _require_admin(current_user)
    req = confirmation_store.approve(confirmation_id, actor=current_user.id)
    if not req:
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    if req.status == ConfirmationStatus.COOLDOWN:
        return {"status": "cooldown", "cooldown_seconds": confirmation_store._cooldown, "message": "Cool-Down aktiv. Erneut bestaetigen nach Ablauf."}
    return {"status": req.status.value}

@app.post("/v1/confirmations/{confirmation_id}/confirm-cooldown", tags=["Action-Engine"])
async def confirm_after_cooldown(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store
    _require_admin(current_user)
    req, remaining = confirmation_store.confirm_after_cooldown(confirmation_id, actor=current_user.id)
    if not req:
        if remaining > 0:
            return {"status": "cooldown", "seconds_remaining": round(remaining, 1)}
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    return {"status": req.status.value}

@app.post("/v1/confirmations/{confirmation_id}/reject", tags=["Action-Engine"])
async def reject_confirmation(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store
    _require_admin(current_user)
    req = confirmation_store.reject(confirmation_id, actor=current_user.id)
    if not req:
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    return {"status": req.status.value}

# ---- Mission-Endpoints (Cloud-Agenten, Setup4) #---

def _mission_to_dict(m: Mission, include_result: bool) -> dict:
    from rag_backend.crypto_utils import decrypt_msg
    d = {
        "id": m.id, "status": m.status, "provider": m.provider,
        "current_step": m.current_step, "calls_used": m.calls_used,
        "tokens_in": m.tokens_in, "tokens_out": m.tokens_out, "error": m.error,
        "origin": getattr(m, "origin", None),
        "owui_chat_id": getattr(m, "owui_chat_id", None),
        "goal": decrypt_msg(m.goal_encrypted),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if include_result:
        d["result"] = decrypt_msg(m.result_encrypted) if m.result_encrypted else None
    return d

@app.get("/v1/missions", tags=["Cloud-Agents"])
async def list_missions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Mission).order_by(Mission.created_at.desc()).limit(50).all()
    return {"missions": [_mission_to_dict(r, include_result=False) for r in rows]}

@app.get("/v1/missions/{mission_id}", tags=["Cloud-Agents"])
async def get_mission(mission_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    return _mission_to_dict(row, include_result=True)

@app.post("/v1/missions/{mission_id}/cancel", tags=["Cloud-Agents"])
async def cancel_mission_endpoint(mission_id: int, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.cloud_agents.missions import request_cancel
    ok = await asyncio.to_thread(request_cancel, mission_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden oder bereits beendet.")
    return {"status": "cancelling"}

# ---- Dashboard-Endpoints #---
#--- Jede Route traegt ihr Gate im Signatur-Kopf: Depends(dashboard_user) =
#--- jedes angemeldete Konto, Depends(dashboard_admin) = nur Administratoren.


def _dashboard_config() -> dict:
    return {
        "embedding_model": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        "rag_k": int(os.getenv("K_RETRIEVAL", "8")),
        "web_timeout_ms": int(float(os.getenv("WEB_SEARCH_TIMEOUT", "25.0")) * 1000),
        #--- Steuert, ob der Argus-Chat den Anhang-Knopf anbietet. Im reinen
        #--- Textbetrieb saehe die Antwort sonst so aus, als haette die Engine das
        #--- Bild angesehen.
        "multimodal": os.getenv("SGLANG_ENABLE_MULTIMODAL", "false").lower() == "true",
        "max_images": _MAX_IMAGES,
        #--- Fuer die Fusszeile des Anhang-Dialogs: womit indexiert wird, soll man
        #--- sehen, BEVOR man auf Ingestieren drueckt.
        "chunk_size": int(os.getenv("CHUNK_SIZE", "1536")),
        "chunk_overlap": int(os.getenv("CHUNK_OVERLAP", "192")),
        "max_upload_mb": _MAX_UPLOAD_BYTES // (1024 * 1024),
    }


@app.get("/v1/dashboard/config", tags=["Dashboard"])
async def dashboard_config(user: User = Depends(dashboard_user)):
    #--- Schlanke Variante der Kennzahlen fuer die Chat-Ansicht.
    return {"model": os.getenv("OAI_MODEL"), "config": _dashboard_config()}


@app.get("/v1/dashboard/summary", tags=["Dashboard"])
async def dashboard_summary(admin: User = Depends(dashboard_admin)):
    snap = metrics.snapshot()
    engine_metrics = await metrics.sglang_metrics()
    tts_metrics_data = await metrics.tts_metrics()
    searxng_up = await metrics.searxng_online()
    #--- Der reale KV-Pool. _serving_budget() fragt ihn einmal bei SGLang ab und
    #--- cacht ihn; ohne diese Zahl ist der Prozentwert im Dashboard bezugslos --
    #--- 34% von 25000 ist etwas anderes als 34% von 7332, und welcher Pool es
    #--- wurde, entscheidet sich bei jedem Start neu.
    try:
        from rag_backend.agent import _serving_budget
        engine_metrics["kv_pool_tokens"] = _serving_budget()
    except Exception as e:
        log.debug(f"KV-Poolgroesse nicht ermittelbar: {e}")
    return {"model": os.getenv("OAI_MODEL"), "backend": snap, "engine": engine_metrics,
            "tts": tts_metrics_data, "searxng_online": searxng_up,
            "config": _dashboard_config()}

@app.get("/v1/dashboard/missions", tags=["Dashboard"])
async def dashboard_missions(db: Session = Depends(get_db),
                             admin: User = Depends(dashboard_admin)):
    rows = db.query(Mission).order_by(Mission.id.desc()).limit(20).all()
    return {"missions": [_mission_to_dict(r, include_result=False) for r in rows]}

@app.get("/v1/dashboard/missions/{mission_id}", tags=["Dashboard"])
async def dashboard_mission_detail(mission_id: int, db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    return _mission_to_dict(row, include_result=True)

#--- Missionen aufraeumen, bewusst nur BEENDETE: an einer laufenden arbeitet der
#--- mission_loop gerade. Zum Stoppen gibt es /cancel.
_MISSION_TERMINAL = ("done", "failed", "cancelled")


@app.delete("/v1/dashboard/missions", tags=["Dashboard"])
async def dashboard_clear_missions(db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin),
                                   csrf = Depends(check_dashboard_csrf)):
    rows = db.query(Mission).filter(Mission.status.in_(_MISSION_TERMINAL)).all()
    count = len(rows)
    for r in rows:
        db.delete(r)
    db.commit()
    aktiv = db.query(Mission).filter(Mission.status.notin_(_MISSION_TERMINAL)).count()
    msg = f"{count} beendete Mission(en) gelöscht."
    if aktiv:
        msg += f" {aktiv} laufende bleiben (erst abbrechen, dann löschen)."
    return {"status": "ok", "deleted": count, "active_kept": aktiv, "message": msg}


@app.delete("/v1/dashboard/missions/{mission_id}", tags=["Dashboard"])
async def dashboard_delete_mission(mission_id: int, db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin),
                                   csrf = Depends(check_dashboard_csrf)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    if row.status not in _MISSION_TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"Mission {mission_id} läuft noch (Status: {row.status}). Erst abbrechen, dann löschen.",
        )
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": mission_id}


@app.get("/v1/dashboard/audit", tags=["Dashboard"])
async def dashboard_audit(db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin)):
    #--- 'details' enthaelt die ausgefuehrte PowerShell im Klartext -- deshalb ist
    #--- diese Route Administratoren vorbehalten.
    rows = db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(50).all()
    return {"audit": [
        {"ts": r.ts.isoformat() if r.ts else None, "tool": r.tool, "tool_class": r.tool_class,
         "status": r.status, "latency_ms": r.latency_ms, "details": r.details} for r in rows
    ]}

@app.get("/v1/dashboard/audit/verify", tags=["Dashboard"])
async def dashboard_audit_verify(admin: User = Depends(dashboard_admin)):
    # Prueft die HMAC-Hash-Chain des Audit-Logs auf Manipulation/Luecken.
    from rag_backend.action_engine.audit import verify_audit_chain
    return verify_audit_chain()

@app.delete("/v1/dashboard/audit", tags=["Dashboard"])
async def dashboard_clear_audit(db: Session = Depends(get_db),
                                admin: User = Depends(dashboard_admin),
                                csrf = Depends(check_dashboard_csrf)):
    try:
        count = db.query(AuditLog).count()
        db.query(AuditLog).delete()
        db.commit()
        return {"status": "ok", "message": f"{count} Audit-Einträge aus der DB gelöscht. Dateien in Action_Audit/ bleiben erhalten."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#--- Freigaben schreibender Host-Aktionen: Administratoren vorbehalten. actor =
#--- die User-ID des Kontos, unter dem der Turn lief. OWNER_CHANNEL waere hier
#--- falsch -- damit liessen sich auch fremde WebUI-Anfragen freigeben.
@app.get("/v1/dashboard/confirmations", tags=["Dashboard"])
async def dashboard_confirmations(admin: User = Depends(dashboard_admin)):
    if os.getenv("ACTION_ENGINE_ENABLED", "false").lower() != "true":
        return {"confirmations": []}
    from rag_backend.action_engine.confirmation import confirmation_store
    pending = confirmation_store.list_pending(actor=admin.id)
    return {"confirmations": [r.model_dump() for r in pending]}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/approve", tags=["Dashboard"])
async def dashboard_confirmation_approve(confirmation_id: str,
                                         admin: User = Depends(dashboard_admin),
                                         csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
    req = confirmation_store.approve(confirmation_id, actor=admin.id, via="chat")
    if not req:
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    if req.status == ConfirmationStatus.COOLDOWN:
        return {"status": "cooldown", "cooldown_seconds": confirmation_store._cooldown,
                "message": "Cool-Down aktiv. Erneut bestaetigen nach Ablauf."}
    return {"status": req.status.value}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/confirm-cooldown", tags=["Dashboard"])
async def dashboard_confirmation_cooldown(confirmation_id: str,
                                          admin: User = Depends(dashboard_admin),
                                          csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store
    req, remaining = confirmation_store.confirm_after_cooldown(
        confirmation_id, actor=admin.id, via="chat")
    if not req:
        if remaining > 0:
            return {"status": "cooldown", "seconds_remaining": round(remaining, 1)}
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    return {"status": req.status.value}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/reject", tags=["Dashboard"])
async def dashboard_confirmation_reject(confirmation_id: str,
                                        admin: User = Depends(dashboard_admin),
                                        csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store
    req = confirmation_store.reject(confirmation_id, actor=admin.id, via="chat")
    if not req:
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    return {"status": req.status.value}


@app.get("/v1/dashboard/state", tags=["Dashboard"])
async def dashboard_state(db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin)):
    return {"indexed_documents": db.query(IngestedDocument).count()}

@app.get("/v1/dashboard/documents", tags=["Dashboard"])
async def list_dashboard_documents(db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin)):
    rows = db.query(IngestedDocument).order_by(IngestedDocument.ingested_at.desc()).all()

    def _groesse(pfad: str | None) -> int | None:
        #--- Aus dem Dateisystem statt aus einer Spalte: eine Migration waere hier
        #--- nur Beiwerk. Ist die Datei inzwischen weg, ist None die ehrliche Antwort.
        if not pfad:
            return None
        try:
            return os.path.getsize(pfad)
        except OSError:
            return None

    return {"documents": [
        {"id": r.id, "filename": r.filename, "chunk_count": r.chunk_count,
         "size_bytes": _groesse(r.file_path),
         "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None} for r in rows
    ]}

@app.delete("/v1/dashboard/documents", tags=["Dashboard"])
async def clear_dashboard_documents(db: Session = Depends(get_db),
                                    admin: User = Depends(dashboard_admin),
                                    csrf = Depends(check_dashboard_csrf)):
    '''Leert die ganze Collection: Qdrant-Chunks und Datensaetze.

    Die Dateien im Arbeitsverzeichnis bleiben liegen -- geloescht wird der INDEX,
    nicht das Original. Wer die Datei loswill, nimmt den Upload-Endpunkt.'''
    rows = db.query(IngestedDocument).all()
    from rag_backend.ingest import _delete_qdrant_chunks
    weg, fehler = 0, 0
    for r in rows:
        try:
            _delete_qdrant_chunks(r.filename)
        except Exception as e:
            fehler += 1
            log.warning(f"Qdrant-Loeschung fuer {r.filename} fehlgeschlagen: {e}")
        db.delete(r)
        weg += 1
    db.commit()
    return {"status": "ok", "deleted": weg, "errors": fehler}


@app.delete("/v1/dashboard/documents/{doc_id}", tags=["Dashboard"])
async def delete_dashboard_document(doc_id: int, db: Session = Depends(get_db),
                                    admin: User = Depends(dashboard_admin),
                                    csrf = Depends(check_dashboard_csrf)):
    doc = db.get(IngestedDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden.")
    filename = doc.filename
    try:
        from rag_backend.ingest import _delete_qdrant_chunks
        _delete_qdrant_chunks(filename)
    except Exception as e:
        log.warning(f"Fehler bei Qdrant-Loeschung fuer {filename}: {e}")
    db.delete(doc)
    db.commit()
    return {"status": "ok", "message": f"Dokument {filename} erfolgreich gelöscht."}

_EVAL_LOGS_DIR = os.getenv("EVAL_LOGS_DIR", "/app/evals/Run_Logs")


def _summarize_eval_file(fpath, fname):
    # Liest eine Run-Log-Datei und extrahiert die Zusammenfassung.
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    suite = d.get("suite")
    if not suite:
        low = fname.lower()
        if low.startswith("gaia"):
            suite = "GAIA-Subset"
        elif low.startswith("humaneval"):
            suite = "HumanEval"
        else:
            suite = fname.rsplit(".", 1)[0]
    metric, value = d.get("metric"), d.get("value")
    if value is None:
        if d.get("accuracy") is not None:
            metric, value = metric or "Accuracy", d.get("accuracy")
        elif d.get("score") is not None:
            metric, value = metric or "pass@1", d.get("score")
    ts = d.get("timestamp") or d.get("ts")
    if not ts:
        try:
            ts = datetime.fromtimestamp(os.stat(fpath).st_mtime, tz=timezone.utc).isoformat()
        except Exception:
            ts = None
    return {"ts": ts, "suite": suite, "config": d.get("config") or d.get("model"),
            "task": d.get("task"), "metric": metric, "value": value,
            "notes": d.get("notes"), "file": fname}


@app.get("/v1/dashboard/evals", tags=["Dashboard"])
async def dashboard_evals(admin: User = Depends(dashboard_admin)):
    # Quelle der Wahrheit sind die Run-Log-Dateien in Run_Logs/, nicht die DB.
    base = os.path.realpath(_EVAL_LOGS_DIR)
    rows = []
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if os.path.isfile(fpath):
                    rows.append(_summarize_eval_file(fpath, fname))
        rows.sort(key=lambda r: (r["ts"] or ""), reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Eval-Logs nicht lesen: {e}")
    return {"evals": rows}


@app.delete("/v1/dashboard/evals", tags=["Dashboard"])
async def dashboard_clear_evals(admin: User = Depends(dashboard_admin),
                                csrf = Depends(check_dashboard_csrf)):
    # Loescht die Run-Log-DATEIEN (bewusste, im Dashboard bestaetigte Aktion).
    base = os.path.realpath(_EVAL_LOGS_DIR)
    deleted = 0
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1
        return {"status": "ok", "message": f"{deleted} Eval-Log-Datei(en) gelöscht."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Raw-Viewer fuer eine einzelne Run-Log-Datei. Das Backend liest die gemounteten
# Dateien direkt.

@app.get("/v1/dashboard/eval_logs", tags=["Dashboard"])
async def dashboard_eval_logs(admin: User = Depends(dashboard_admin)):
    base = os.path.realpath(_EVAL_LOGS_DIR)
    files = []
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if not os.path.isfile(fpath):
                    continue
                st = os.stat(fpath)
                files.append({
                    "name": fname,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                })
        files.sort(key=lambda r: r["mtime"], reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Run-Logs nicht lesen: {e}")
    return {"dir": base, "files": files}

@app.get("/v1/dashboard/eval_logs/{name}", tags=["Dashboard"])
async def dashboard_eval_log(name: str, admin: User = Depends(dashboard_admin)):
    # Path-Traversal-Schutz: nur Basename, nur .json, und die aufgeloeste Datei
    # MUSS direkt im Run_Logs-Verzeichnis liegen.
    if name != os.path.basename(name) or not name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname.")
    base = os.path.realpath(_EVAL_LOGS_DIR)
    target = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(target) != base or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Log-Datei nicht gefunden.")
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Log nicht lesen: {e}")

# ---- Voice-Konsole: Dashboard-Chat mit Orb-Events #---

class DashboardChatIn(BaseModel):
    message: str
    #--- KEIN history-Feld: der Verlauf kommt ausschliesslich aus der DB
    #--- (_chat_history_from_db an der session_id).
    session_id: int | None = None
    #--- data:-URIs aus Dateidialog oder Zwischenablage. Gefiltert nach denselben
    #--- Regeln wie im OpenAI-Proxy.
    images: list[str] = []


def _dashboard_images(raw) -> list:
    '''Wie extract_image_urls, nur fuer die flache Liste des Argus-Chats.

    Dieselben zwei Grenzen (Anzahl und Groesse je URI) und dieselbe Beschraenkung auf
    data:image -- eine http-URL wuerde das Modell-Backend dazu bringen, im Namen des
    Nutzers eine fremde Adresse abzurufen (SSRF ueber den Umweg Bild).'''
    out = []
    for u in (raw or []):
        if not isinstance(u, str) or not u.startswith("data:image/"):
            continue
        if len(u) > _MAX_IMAGE_DATA_CHARS:
            continue
        out.append(u)
        if len(out) >= _MAX_IMAGES:
            break
    return out


def _orb_sse(d: dict) -> str:
    return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


def _chat_title(db, session_id: int) -> str:
    # Titel = erste Nutzernachricht, gekuerzt. Bewusst ohne eigene title-Spalte:
    # eine Migration hier wuerde die Alembic-Kette ohne Setup4 zerreissen.
    m = (db.query(Message).filter_by(session_id=session_id, sender="user")
         .order_by(Message.id.asc()).first())
    if not m:
        return "Neuer Chat"
    try:
        first_line = decrypt_msg(m.content_encrypted).strip().splitlines()[0]
    except Exception as e:
        # Mit lautem Log, nicht still: ein zu breites except hat hier schon einen
        # NameError als Krypto-Problem getarnt.
        log.warning(f"Chat-Titel fuer Session {session_id} nicht lesbar: {type(e).__name__}: {e}")
        return "Neuer Chat"
    t = re.sub(r"\s+", " ", first_line).strip()
    if not t:
        return "Neuer Chat"
    return t[:60] + "…" if len(t) > 60 else t


def _chat_history_from_db(db, session_id: int, limit: int = 10) -> list:
    # Verlauf kommt aus der DB, nicht vom Client: ein manipulierter Client
    # koennte dem Agenten sonst eine Vorgeschichte unterschieben.
    rows = (db.query(Message).filter_by(session_id=session_id)
            .order_by(Message.id.desc()).limit(limit).all())
    out = []
    for m in reversed(rows):
        try:
            content = decrypt_msg(m.content_encrypted)
        except Exception as e:
            log.warning(f"Verlauf: Nachricht {m.id} uebersprungen ({type(e).__name__}: {e})")
            continue
        if not content:
            continue
        if m.sender == "user":
            out.append(HumanMessage(content=content))
        else:
            out.append(AIMessage(content=strip_status_lines(content)))
    return out


@app.get("/v1/dashboard/chats", tags=["Dashboard"])
def dashboard_chats(db: Session = Depends(get_db), user: User = Depends(dashboard_user)):
    #--- Verlaeufe sind an das Konto gebunden. Der Agent haengt sein Gedaechtnis
    #--- an dieselbe ID -- jedes Konto hat damit sein eigenes.
    rows = (db.query(ChatSession).filter_by(user_id=user.id)
            .order_by(ChatSession.created_at.desc()).limit(200).all())
    out = []
    for s in rows:
        last = (db.query(Message).filter_by(session_id=s.id)
                .order_by(Message.id.desc()).first())
        out.append({
            "id": s.id,
            "title": _chat_title(db, s.id),
            "messages": db.query(Message).filter_by(session_id=s.id).count(),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_at": last.timestamp.isoformat() if last and last.timestamp else None,
        })
    return {"chats": out}


@app.get("/v1/dashboard/chats/{chat_id}", tags=["Dashboard"])
def dashboard_chat_load(chat_id: int, db: Session = Depends(get_db),
                        user: User = Depends(dashboard_user)):
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    msgs = db.query(Message).filter_by(session_id=chat_id).order_by(Message.id.asc()).all()
    out = []
    for m in msgs:
        try:
            content = decrypt_msg(m.content_encrypted)
        except Exception as e:
            # Fernet-Key gewechselt: die einzelne Zeile bleibt sichtbar, statt den
            # ganzen Verlauf unlesbar zu machen. Mit Log.
            log.warning(f"Nachricht {m.id} nicht entschluesselbar: {type(e).__name__}: {e}")
            content = "[nicht entschluesselbar]"
        #--- Werkzeugschritte sind Beiwerk: ist die Spalte leer oder der Schluessel
        #--- gewechselt, fehlt eben der Block. Die Nachricht selbst bleibt lesbar.
        steps = None
        if getattr(m, "steps_encrypted", None):
            try:
                steps = json.loads(decrypt_msg(m.steps_encrypted))
            except Exception as e:
                log.warning(f"Schritte zu Nachricht {m.id} nicht lesbar: {type(e).__name__}")
        out.append({
            #--- id mitliefern: die Oberflaeche haengt Aktionen (bearbeiten, erneut
            #--- versuchen, loeschen) an die einzelne Nachricht und braucht dafuer
            #--- einen Bezug, der einen Neuaufbau des Verlaufs uebersteht.
            "id": m.id,
            "role": "user" if m.sender == "user" else "assistant",
            "content": content,
            "steps": steps,
            #--- Dauer der Antwort: die Chat-Ansicht zeigt sie im Nachrichtenkopf
            #--- neben der Uhrzeit. Steht nur an Assistenz-Nachrichten.
            "latency_ms": m.latency_ms,
            "ts": m.timestamp.isoformat() if m.timestamp else None,
        })
    return {"id": s.id, "title": _chat_title(db, s.id), "messages": out}


@app.delete("/v1/dashboard/chats/{chat_id}/messages/{message_id}", tags=["Dashboard"])
def dashboard_message_delete(chat_id: int, message_id: int, following: bool = False,
                             db: Session = Depends(get_db),
                             user: User = Depends(dashboard_user),
                             csrf = Depends(check_dashboard_csrf)):
    '''Einzelne Nachricht loeschen, optional mitsamt allem, was danach kam.

    following=True ist der Fall "nochmal versuchen" und "Frage bearbeiten": eine
    Antwort verwerfen heisst auch, alles zu verwerfen, was auf ihr aufbaut -- sonst
    stuenden im Verlauf Folgeantworten zu einer Frage, die es nicht mehr gibt.
    Die Session-ID muss passen: sonst liesse sich ueber eine fremde chat_id jede
    beliebige Nachricht loeschen.
    '''
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    msg = db.query(Message).filter_by(id=message_id, session_id=chat_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Nachricht nicht gefunden")
    if following:
        # Reihenfolge ueber die ID, nicht ueber den Zeitstempel.
        q = db.query(Message).filter(Message.session_id == chat_id,
                                     Message.id >= message_id)
    else:
        q = db.query(Message).filter(Message.id == message_id)
    count = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return {"status": "ok", "deleted": count}


@app.post("/v1/dashboard/chats", tags=["Dashboard"])
def dashboard_chat_new(db: Session = Depends(get_db), user: User = Depends(dashboard_user),
                       csrf = Depends(check_dashboard_csrf)):
    s = ChatSession(user_id=user.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "title": "Neuer Chat", "messages": []}


@app.delete("/v1/dashboard/chats/{chat_id}", tags=["Dashboard"])
def dashboard_chat_delete(chat_id: int, db: Session = Depends(get_db),
                          user: User = Depends(dashboard_user),
                          csrf = Depends(check_dashboard_csrf)):
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    # Nachrichten zuerst: auf messages.session_id liegt ein Fremdschluessel.
    db.query(Message).filter_by(session_id=chat_id).delete()
    db.delete(s)
    db.commit()
    return {"status": "deleted", "id": chat_id}


@app.post("/v1/dashboard/chat", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_CHAT_RATE_LIMIT", "60/minute"))
async def dashboard_chat(body: DashboardChatIn, request: Request,
                         user: User = Depends(dashboard_user),
                         csrf = Depends(check_dashboard_csrf)):
    # Streamt getypte SSE-Events, die die Orb-Zustaende treiben: state / reasoning
    # / tool / token / sources / done.
    uid = user.id

    #--- Reihenfolge zaehlt: erst den bisherigen Verlauf lesen, DANN die neue
    #--- Nachricht schreiben -- sonst stuende die Frage doppelt im Kontext.
    with SessionLocal() as db0:
        chat_session = None
        if body.session_id:
            chat_session = db0.query(ChatSession).filter_by(
                id=body.session_id, user_id=uid).first()
        if not chat_session:
            chat_session = ChatSession(user_id=uid)
            db0.add(chat_session)
            db0.commit()
            db0.refresh(chat_session)
        sid = chat_session.id
        chat_history = _chat_history_from_db(db0, sid)
        #--- Bildmarke wie im Proxy, sonst steht im Verlauf eine leere Nutzerzeile.
        images = _dashboard_images(body.images)
        db0.add(Message(session_id=sid, sender="user",
                        content_encrypted=encrypt_msg(
                            ("[Bild angehaengt] " if images else "") + body.message)))
        db0.commit()

    agent_payload = {
        "input": body.message,
        "chat_history": chat_history,
        "user_id": uid,
        "images": images,
        #--- Herkunft fuer die Missions-Zustellung. Ohne diese Felder meldete eine
        #--- hier gestartete Mission ihr Ergebnis per Telegram.
        "channel": "dashboard",
        "dashboard_session_id": sid,
    }

    async def gen():
        #--- Laufzeitmessung wie im Proxy. Ohne sie zaehlte der Dashboard-Chat in
        #--- keine Request-Kennzahl ein.
        _t0 = time.monotonic()
        _ttft_ms = None
        _recorded = False
        stream = agent_executor.astream(agent_payload)
        first_answer = True
        answer_parts = []
        #--- Mitschrift des Turns. Sie geht am Ende verschluesselt in die Datenbank,
        #--- damit ein neu geladener Verlauf nicht nur den nackten Antworttext zeigt.
        schritte = []
        quellen = []
        #--- Laufender Werkzeugschritt: (Startzeit, Index in schritte). Seine Dauer
        #--- steht erst fest, wenn etwas anderes kommt -- dann wird er geschlossen.
        offener_schritt = None

        def _record_once():
            #--- Genau einmal je Turn. Zaehlt die ROH gestreamte Ausgabe, nicht die
            #--- bereinigte -- der Proxy zaehlt ebenso.
            nonlocal _recorded
            if _recorded:
                return
            _recorded = True
            _raw = "".join(answer_parts)
            _total = round((time.monotonic() - _t0) * 1000.0)
            try:
                metrics.record_request(
                    ttft_ms=round(_ttft_ms) if _ttft_ms is not None else None,
                    total_ms=_total,
                    completion_chars=len(_raw),
                )
            except Exception:
                pass

        try:
            # Eigener Event-Typ, KEIN state-Event: state-Werte gehen direkt in
            # setOrbState(), ein unbekannter Wert wuerde den Orb zuruecksetzen.
            yield _orb_sse({"type": "session", "session_id": sid})
            yield _orb_sse({"type": "state", "value": "thinking"})
            async for chunk in stream:
                if await request.is_disconnected():
                    break
                if not isinstance(chunk, dict):
                    continue
                #--- Freigabe-Anfrage: die Chat-Ansicht baut daraus eine Karte mit
                #    Knoepfen. Muss VOR den uebrigen Zweigen stehen -- ein
                #    confirmation-Chunk hat weder status noch output.
                conf = chunk.get("confirmation")
                if conf:
                    yield _orb_sse({"type": "confirmation", **conf})
                    continue
                #--- Jeder andere Chunk beendet den laufenden Werkzeugschritt.
                if offener_schritt is not None:
                    _t, _i = offener_schritt
                    _ms = round((time.monotonic() - _t) * 1000)
                    schritte[_i]["dauer_ms"] = _ms
                    offener_schritt = None
                    yield _orb_sse({"type": "tool_done", "index": _i, "dauer_ms": _ms})

                reasoning_chunk = chunk.get("reasoning")
                if reasoning_chunk:
                    schritte.append({"art": "denken", "text": reasoning_chunk})
                    yield _orb_sse({"type": "reasoning", "text": reasoning_chunk})
                    continue
                status = chunk.get("status")
                if status:
                    # TTFT = erste SICHTBARE Ausgabe. Eine Statuszeile zaehlt mit,
                    # ein Reasoning-Chunk nicht -- gleiche Abgrenzung wie im Proxy.
                    if _ttft_ms is None:
                        _ttft_ms = (time.monotonic() - _t0) * 1000.0
                    # Erstes Zeichen der Statuszeile ist das Anzeige-Emoji.
                    schritte.append({"art": "werkzeug", "emoji": status[0], "text": status.strip()})
                    offener_schritt = (time.monotonic(), len(schritte) - 1)
                    yield _orb_sse({"type": "tool", "emoji": status[0], "label": status.strip(),
                                    "index": len(schritte) - 1})
                    continue
                #--- Quellen des Turns, durchlaufend nummeriert im Agenten. Sie tragen
                #--- die Nummern, mit denen die Antwort sie belegt.
                srcs = chunk.get("sources")
                if srcs:
                    quellen.extend(srcs)
                    yield _orb_sse({"type": "sources", "items": srcs})
                    continue
                out = chunk.get("output")
                if not out:
                    continue
                if _ttft_ms is None:
                    _ttft_ms = (time.monotonic() - _t0) * 1000.0
                if first_answer:
                    first_answer = False
                    yield _orb_sse({"type": "state", "value": "speaking"})
                answer_parts.append(out)
                yield _orb_sse({"type": "token", "text": out})

            #--- Letzter Schritt: er wird von keinem weiteren Chunk mehr geschlossen.
            if offener_schritt is not None:
                _t, _i = offener_schritt
                _ms = round((time.monotonic() - _t) * 1000)
                schritte[_i]["dauer_ms"] = _ms
                offener_schritt = None
                yield _orb_sse({"type": "tool_done", "index": _i, "dauer_ms": _ms})

            #--- Kennzahlen JETZT erfassen: der Missions-Follow wartet u.U. Minuten
            #--- auf einen Cloud-Worker, das ist keine Antwortlatenz.
            _record_once()

            #--- Mission-Follow wie im Proxy: hat dieser Turn eine Mission gestartet,
            #--- bleibt der Stream offen. Der Argus-Chat hat keine Push-API.
            if os.getenv("CLOUD_MISSION_FOLLOW", "true").lower() == "true":
                try:
                    from rag_backend.cloud_agents import missions as _missions
                    _follow_ids = await asyncio.to_thread(_missions.active_for_dashboard, sid)
                except Exception as e:
                    log.warning(f"Argus-Chat Mission-Follow: Start fehlgeschlagen: {e}")
                    _follow_ids = []
                if _follow_ids:
                    # Registrierung VOR dem ersten Poll: ab jetzt ueberspringt
                    # _deliver die Zustellung fuer diese IDs.
                    _missions._FOLLOWED.update(_follow_ids)
                    _streamed = set()
                    try:
                        _deadline = time.monotonic() + int(os.getenv("CLOUD_MISSION_FOLLOW_MAX_SECONDS", "240"))
                        _poll = max(2, int(os.getenv("CLOUD_MISSION_FOLLOW_POLL_SECONDS", "4")))
                        _pending = list(_follow_ids)
                        _last_step: dict = {}
                        log.info(f"Argus-Chat Mission-Follow: begleite {_pending} in Session {sid}.")
                        while _pending and time.monotonic() < _deadline:
                            if await request.is_disconnected():
                                log.info("Argus-Chat Mission-Follow: Client weg -- Zustellung uebernimmt.")
                                break
                            # verhindert Reflection-Start mitten im Follow
                            request.app.state.last_activity = datetime.now(timezone.utc)
                            for _mid in list(_pending):
                                _status, _step = await asyncio.to_thread(_missions.mission_step, _mid)
                                if _status == "missing":
                                    _pending.remove(_mid)
                                    continue
                                if _status in _missions._TERMINAL_NEEDLES:
                                    _text = await asyncio.to_thread(_missions.delivery_text, _mid)
                                    if _text:
                                        # Ergebnis streamen UND in answer_parts,
                                        # damit es mitpersistiert wird.
                                        yield _orb_sse({"type": "token", "text": "\n\n" + _text})
                                        answer_parts.append("\n\n" + _text)
                                    _streamed.add(_mid)
                                    _pending.remove(_mid)
                                    _missions._FOLLOWED.discard(_mid)
                                    continue
                                if _step and _last_step.get(_mid) != _step:
                                    _last_step[_mid] = _step
                                    # Fortschritt als Werkzeug-Schritt: sichtbar als
                                    # Zeile, aber nicht Teil der Antwort.
                                    yield _orb_sse({"type": "tool", "emoji": "⏳",
                                                    "label": f"⏳ Mission {_mid}: {_step}"})
                            if _pending:
                                await asyncio.sleep(_poll)
                        if _pending and not await request.is_disconnected():
                            _ids = ", ".join(str(i) for i in _pending)
                            _hint = (f"\n\nMission {_ids} läuft weiter im Hintergrund -- "
                                     "das Ergebnis erscheint automatisch hier im Chat.")
                            yield _orb_sse({"type": "token", "text": _hint})
                            answer_parts.append(_hint)
                    except Exception as e:
                        # Follow ist Komfort -- ein Fehler darf den Turn nicht abreissen.
                        log.warning(f"Argus-Chat Mission-Follow abgebrochen: {e}")
                    finally:
                        _missions._FOLLOWED.difference_update(_follow_ids)
                        #--- Schliesst das Rennen zwischen Abmeldung und Missions-
                        #--- Ende: sonst uebersprang _deliver die Zustellung, waehrend
                        #--- hier schon niemand mehr zuhoert.
                        for _mid in _follow_ids:
                            if _mid in _streamed:
                                continue
                            try:
                                _st, _ = await asyncio.to_thread(_missions.mission_step, _mid)
                                if _st in _missions._TERMINAL_NEEDLES:
                                    _txt = await asyncio.to_thread(_missions.delivery_text, _mid)
                                    if _txt:
                                        await asyncio.to_thread(
                                            _missions._append_dashboard_message, sid, _txt)
                            except Exception as e:
                                log.warning(f"Nachreichen von Mission {_mid} fehlgeschlagen: {e}")

            yield _orb_sse({"type": "done", "session_id": sid})
        finally:
            await stream.aclose()
            # Abgebrochener Turn (Tab zu, Stop-Knopf): er hat GPU-Zeit gekostet und
            # gehoert in den Schnitt.
            _record_once()
            raw_output = "".join(answer_parts)
            total_ms = round((time.monotonic() - _t0) * 1000.0)
            ttft_val = round(_ttft_ms) if _ttft_ms is not None else None
            tokens_sec_val = None
            if total_ms and raw_output:
                secs = total_ms / 1000.0
                if secs > 0:
                    tokens_sec_val = round((len(raw_output) / metrics._CHARS_PER_TOKEN) / secs, 1)
            # Auch bei Abbruch persistieren: die Teilantwort gehoert zum Verlauf.
            # Eigene DB-Session -- die des Requests ist hier zu.
            answer = strip_status_lines(raw_output).strip()
            if answer:
                try:
                    with SessionLocal() as db2:
                        #--- Denk-Chunks zu EINEM Block zusammenziehen: sie kommen
                        #--- tokenweise, einzeln gespeichert waeren es Hunderte Eintraege.
                        gefaltet, denk = [], []
                        for sch in schritte:
                            if sch["art"] == "denken":
                                denk.append(sch["text"])
                                continue
                            if denk:
                                gefaltet.append({"art": "denken", "text": "".join(denk)})
                                denk = []
                            gefaltet.append(sch)
                        if denk:
                            gefaltet.append({"art": "denken", "text": "".join(denk)})
                        mitschrift = encrypt_msg(json.dumps(
                            {"schritte": gefaltet, "quellen": quellen},
                            ensure_ascii=False)) if (gefaltet or quellen) else None
                        db2.add(Message(session_id=sid, sender="assistant",
                                        content_encrypted=encrypt_msg(answer),
                                        steps_encrypted=mitschrift,
                                        latency_ms=total_ms, ttft_ms=ttft_val,
                                        tokens_per_sec=tokens_sec_val))
                        db2.commit()
                except Exception as e:
                    log.warning(f"Dashboard-Chat: Antwort nicht persistierbar: {e}")

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/dashboard/stt", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_VOICE_RATE_LIMIT", "60/minute"))
async def dashboard_stt(request: Request, file: UploadFile = File(...),
                        user: User = Depends(dashboard_user),
                        csrf = Depends(check_dashboard_csrf)):
    # Browser nimmt Audio auf (MediaRecorder) -> rag-backend -> stt-service.
    import httpx
    url = os.getenv("STT_SERVICE_URL", "http://stt-service:8003").rstrip("/") + "/v1/audio/transcriptions"
    data = await file.read()
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(url, files={"file": (file.filename or "audio.webm", data, file.content_type or "audio/webm")})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STT-Dienst nicht erreichbar: {e}")


class DashboardTtsIn(BaseModel):
    input: str
    voice: str | None = None


@app.post("/v1/dashboard/tts", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_VOICE_RATE_LIMIT", "60/minute"))
async def dashboard_tts(body: DashboardTtsIn, request: Request,
                        user: User = Depends(dashboard_user),
                        csrf = Depends(check_dashboard_csrf)):
    # Antwort-Text -> tts-service (intern) -> Audio-Bytes, vom Frontend abgespielt.
    import httpx
    url = os.getenv("TTS_SERVICE_URL", "http://tts-service:8002").rstrip("/") + "/v1/audio/speech"
    payload = {"model": os.getenv("AUDIO_TTS_MODEL", "tts-1"), "input": body.input,
               "voice": body.voice or os.getenv("AUDIO_TTS_VOICE", "alloy")}
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(url, json=payload)
        r.raise_for_status()
        return Response(content=r.content, media_type=r.headers.get("content-type", "audio/wav"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS-Dienst nicht erreichbar: {e}")


# ---- Dateien: hochladen, indexieren #---
#--- Ziel ist der Uploads-Ordner des Arbeitsverzeichnisses. Genau dort darf der
#--- Agent lesen -- eine Datei anderswo im Workspace umginge die Pfad-Whitelist.

_WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_CONTAINER_DIR", "/host/argus_workspace"))
_UPLOAD_DIR = _WORKSPACE_ROOT / "Uploads"
_MAX_UPLOAD_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(500 * 1024 * 1024)))

#--- Aufraeumen. In den Chat gezogene Dateien sind Arbeitsmaterial fuer EINE
#--- Unterhaltung; was dauerhaft auffindbar bleiben soll, gehoert ueber
#--- ingest_document in den Index, nicht in diesen Ordner. Ohne Frist saehe man
#--- hier nach einem halben Jahr jeden je geprueften Vertrag liegen.
#--- Sofort nach dem Lesen loeschen geht NICHT: die erste Rueckfrage in derselben
#--- Unterhaltung liesse read_document sonst ins Leere laufen.
#--- 0 Stunden = Aufraeumen aus.
_UPLOAD_RETENTION_HOURS = float(os.getenv("UPLOAD_RETENTION_HOURS", "24"))
_UPLOAD_SWEEP_SECONDS = int(os.getenv("UPLOAD_SWEEP_SECONDS", "3600"))


def _sweep_uploads_once() -> int:
    '''Entfernt Dateien im Uploads-Ordner, die aelter als die Frist sind.
    Liefert die Anzahl. Unterordner bleiben unangetastet.'''
    if _UPLOAD_RETENTION_HOURS <= 0:
        return 0
    try:
        if not _UPLOAD_DIR.is_dir():
            return 0
        eintraege = list(_UPLOAD_DIR.iterdir())
    except Exception:
        return 0
    grenze = time.time() - _UPLOAD_RETENTION_HOURS * 3600
    weg = 0
    for p in eintraege:
        try:
            if not p.is_file() or p.stat().st_mtime >= grenze:
                continue
            p.unlink()
            weg += 1
            log.info(f"Upload aufgeraeumt (aelter als {_UPLOAD_RETENTION_HOURS}h): {p.name}")
        except Exception as e:
            log.warning(f"Upload {p.name} liess sich nicht entfernen: {e}")
    return weg


async def _uploads_sweeper():
    '''Einmal beim Start, danach im Takt. Ein Fehler beendet die Schleife nicht --
    Aufraeumen ist Pflege, kein Betriebszustand.'''
    while True:
        try:
            n = await asyncio.to_thread(_sweep_uploads_once)
            if n:
                log.info(f"Uploads aufgeraeumt: {n} Datei(en) entfernt.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"Uploads-Aufraeumen fehlgeschlagen: {e}")
        await asyncio.sleep(_UPLOAD_SWEEP_SECONDS)
#--- Endungs-Allowlist statt Blocklist, deckungsgleich mit dem, was load_file
#--- lesen kann, plus Bilder. .env/.log fehlen bewusst -- solche Dateien tragen
#--- typischerweise Secrets.
#--- Archive sind bewusst nicht dabei: Pfad-Ausbruch, Kompressionsbomben und
#--- Dateizahl muessten alle einzeln abgefangen werden.
_UPLOAD_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".epub", ".txt", ".md",
    ".html", ".htm", ".json", ".yaml", ".yml", ".csv", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}
#--- Dateitypen, aus denen die Indexierung Text ziehen kann.
_INGESTABLE_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".epub", ".txt", ".md",
    ".html", ".htm", ".json", ".yaml", ".yml", ".csv",
}


def _safe_upload_name(raw: str) -> str:
    '''Baut aus einem beliebigen Client-Dateinamen einen harmlosen Basename.

    Ein Upload-Name kommt vom Browser und ist damit Fremdeingabe: '../../run/secrets/
    ssh_key' oder ein Name mit Doppelpunkt (alternativer NTFS-Datenstrom) muss hier
    scheitern, nicht erst am Dateisystem.'''
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", name).lstrip(".")
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Unbrauchbarer Dateiname.")
    return name[:180]


def _upload_path(name: str) -> Path:
    '''Loest einen Namen im Uploads-Ordner auf und stellt sicher, dass er dort bleibt.'''
    target = (_UPLOAD_DIR / _safe_upload_name(name)).resolve(strict=False)
    try:
        target.relative_to(_UPLOAD_DIR.resolve(strict=False))
    except ValueError:
        raise HTTPException(status_code=400, detail="Pfad liegt ausserhalb des Uploads-Ordners.")
    return target


def _upload_entry(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "is_dir": p.is_dir(),
        "ingestable": p.is_dir() or p.suffix.lower() in _INGESTABLE_EXTENSIONS,
    }


@app.get("/v1/dashboard/uploads", tags=["Dashboard"])
async def dashboard_uploads(admin: User = Depends(dashboard_admin)):
    if not _UPLOAD_DIR.is_dir():
        return {"dir": str(_UPLOAD_DIR), "files": []}
    entries = []
    for p in sorted(_UPLOAD_DIR.iterdir(), key=lambda x: x.name.lower()):
        try:
            entries.append(_upload_entry(p))
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"dir": str(_UPLOAD_DIR), "files": entries, "max_bytes": _MAX_UPLOAD_BYTES,
            "extensions": sorted(_UPLOAD_EXTENSIONS)}


@app.post("/v1/dashboard/uploads", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_UPLOAD_RATE_LIMIT", "30/minute"))
async def dashboard_upload(request: Request, file: UploadFile = File(...),
                           admin: User = Depends(dashboard_admin),
                           csrf = Depends(check_dashboard_csrf)):
    name = _safe_upload_name(file.filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in _UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=415,
                            detail=f"Dateityp '{suffix or '?'}' ist nicht zugelassen.")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = _upload_path(name)
    #--- Bestehende Datei nicht ueberschreiben, sondern durchnummerieren.
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        for i in range(2, 100):
            target = _upload_path(f"{stem}_{i}{ext}")
            if not target.exists():
                break
    #--- In Bloecken schreiben und mitzaehlen: ein content-length-Header ist
    #--- Fremdeingabe, die ankommenden Bytes sind es nicht.
    written = 0
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Datei groesser als {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Schreiben fehlgeschlagen: {e}")
    log.info(f"Upload durch '{admin.email}': {target.name} ({written} Bytes)")
    return {"status": "ok", **_upload_entry(target)}


@app.post("/v1/dashboard/uploads/ingest_all", tags=["Dashboard"])
async def dashboard_upload_ingest_all(admin: User = Depends(dashboard_admin),
                                      csrf = Depends(check_dashboard_csrf)):
    '''Indexiert alle ingestierbaren Dateien im Uploads-Ordner.

    Ruft je Datei denselben Weg wie der Einzel-Endpunkt auf -- eine zweite
    Ingest-Logik waere eine zweite Quelle fuer dieselbe Frage, wie ein Dokument
    zerlegt wird. Nicht ingestierbare Endungen (Bilder) werden uebersprungen,
    nicht als Fehler gemeldet.'''
    if not _UPLOAD_DIR.is_dir():
        return {"status": "ok", "verarbeitet": 0, "indexiert": 0, "ergebnisse": []}
    kandidaten = sorted(
        (p for p in _UPLOAD_DIR.iterdir()
         if p.is_dir() or p.suffix.lower() in _INGESTABLE_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )
    ergebnisse, indexiert = [], 0
    for p in kandidaten:
        try:
            r = await dashboard_upload_ingest(p.name, admin=admin, csrf=csrf)
            indexiert += r.get("indexed", 0)
            ergebnisse.append({"name": p.name, "status": "ok",
                               "indexed": r.get("indexed", 0), "total": r.get("total", 0)})
        except HTTPException as e:
            ergebnisse.append({"name": p.name, "status": "fehler", "detail": str(e.detail)})
    return {"status": "ok", "verarbeitet": len(ergebnisse), "indexiert": indexiert,
            "ergebnisse": ergebnisse}


@app.post("/v1/dashboard/uploads/{name}/ingest", tags=["Dashboard"])
async def dashboard_upload_ingest(name: str, admin: User = Depends(dashboard_admin),
                                  csrf = Depends(check_dashboard_csrf)):
    '''Indexiert eine hochgeladene Datei oder einen entpackten Ordner in Qdrant.

    Nutzt dieselben Bausteine wie der Stapel-Ingest und das ingest_document-Werkzeug
    (load_file zum Lesen, _store_text_in_qdrant zum Chunken und Ablegen) -- damit ein
    ueber das Dashboard indexiertes Dokument identisch zerlegt wird wie eines aus
    documents_to_ingest.'''
    from rag_backend.agent import _store_text_in_qdrant
    from rag_backend.ingest import load_file

    target = _upload_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")

    files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
    if not files:
        raise HTTPException(status_code=400, detail="Der Ordner enthaelt keine Dateien.")

    results, ok_count = [], 0
    for f in files:
        rel = f.relative_to(target) if target.is_dir() else Path(f.name)
        label = str(rel).replace("/", "_").replace("\\", "_")
        try:
            text = await asyncio.to_thread(load_file, f)
            if not text or not text.strip():
                results.append({"file": label, "status": "leer",
                                "message": "Kein Text lesbar (Bild-PDF oder leeres Dokument?)."})
                continue
            msg = await _store_text_in_qdrant(
                label, text, source=f"C:\\Argus_Workspace\\Uploads\\{name}")
            failed = msg.startswith("ERROR")
            results.append({"file": label, "status": "fehler" if failed else "ok",
                            "message": msg})
            if not failed:
                ok_count += 1
        except Exception as e:
            results.append({"file": label, "status": "fehler", "message": str(e)})

    log.info(f"Indexierung durch '{admin.email}': {name} ({ok_count}/{len(files)} erfolgreich)")
    return {"status": "ok", "indexed": ok_count, "total": len(files), "results": results}


@app.delete("/v1/dashboard/uploads/{name}", tags=["Dashboard"])
async def dashboard_upload_delete(name: str, admin: User = Depends(dashboard_admin),
                                  csrf = Depends(check_dashboard_csrf)):
    import shutil

    target = _upload_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")
    #--- Loescht nur die Datei. Indexierte Inhalte raeumt die Dokumentenliste ab.
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "deleted", "name": target.name}


# ---- OpenAI-Proxy: /v1/chat/completions #---

@app.post("/v1/chat/completions", tags=["OpenAI Proxy"])
@limiter.limit(os.getenv("CHAT_RATE_LIMIT", "60/minute"))
async def chat_completions(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    csrf = Depends(check_csrf_headers),
):
    body = await request.json()
    request.app.state.last_activity = datetime.now(timezone.utc)
    # Herkunft fuer "Ergebnis zurueck zur Quelle": OpenWebUI reicht diese Header
    # bei ENABLE_FORWARD_USER_INFO_HEADERS=true durch.
    _owui_meta = {
        "channel": "webui",
        "owui_chat_id": request.headers.get("x-openwebui-chat-id"),
        "owui_message_id": request.headers.get("x-openwebui-message-id"),
        "owui_user_id": request.headers.get("x-openwebui-user-id"),
    }
    log.info(f"OWUI-Header: chat_id={_owui_meta['owui_chat_id']} "
             f"msg_id={_owui_meta['owui_message_id']} user_id={_owui_meta['owui_user_id']}")
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages found.")

    user_message_content = extract_text_content(messages[-1].get("content", ""))
    user_images = extract_image_urls(messages[-1].get("content", ""))
    # "Antwort fortsetzen" schickt den Verlauf MIT der eigenen Assistant-Nachricht
    # als letztem Element -- das ist keine neue Nutzerfrage.
    _is_continue = bool(messages) and messages[-1].get("role") == "assistant"
    if _is_continue:
        user_message_content = (
            "(The user pressed 'continue response' -- there is no new question.) "
            "Continue your last answer directly without repeating what was said; "
            "if there is nothing to continue, briefly summarize the current state."
        )
        user_images = []
    _stripped_input = user_message_content.strip()
    # OWUI nutzt "### Task:" fuer interne Meta-Tasks UND fuer die RAG-Antwort.
    # Nur die Meta-Tasks duerfen in den JSON-Zweig.
    _is_rag_answer = _stripped_input.startswith("### Task:") and "respond to the user query" in _stripped_input[:200].lower()
    if _is_rag_answer:
        user_message_content = _stripped_input[len("### Task:"):].lstrip()
    is_background_task = _stripped_input.startswith("### Task:") and not _is_rag_answer

    if is_background_task:
        agent_payload = {"input": user_message_content, "chat_history": [], "user_id": current_user.id}
        if body.get("stream", False):
            async def stream_task():
                stream = agent_executor.astream(agent_payload)
                try:
                    async for chunk in stream:
                        if await request.is_disconnected():
                            log.info("Client disconnected (background) — breche Stream ab.")
                            break
                        if not isinstance(chunk, dict) or chunk.get("status"):
                            continue
                        raw_chunk = chunk.get("output")
                        if not raw_chunk:
                            continue
                        content_chunk = clean_agent_output(raw_chunk)
                        if not content_chunk:
                            continue
                        yield _sse_delta(content_chunk)
                    yield "data: [DONE]\n\n"
                finally:
                    await stream.aclose()
            return StreamingResponse(stream_task(), media_type="text/event-stream")

        result = await agent_executor.ainvoke(agent_payload)
        return JSONResponse(_completion_json(
            {"role": "assistant", "content": clean_agent_output(result.get("output"))}
        ))

    chat_history = []
    # History bleibt TEXT-ONLY: Bilder frueherer Turns wuerden je ~1600 Tokens
    # des KV-Pools fressen. Nur Bilder der AKTUELLEN Nachricht gehen ans Modell.
    for m in (messages if _is_continue else messages[:-1]):
        if m.get("role") == "user":
            chat_history.append(HumanMessage(content=extract_text_content(m.get("content", ""))))
        elif m.get("role") == "assistant":
            chat_history.append(AIMessage(content=strip_status_lines(extract_text_content(m.get("content", "")))))

    # Missions-Nachlieferung (zweites Netz zum Live-Push): woertlich aus der DB,
    # ohne LLM-Umweg.
    _bridge_text = ""
    if _owui_meta.get("owui_chat_id"):
        try:
            from rag_backend.cloud_agents.missions import undelivered_for_chat
            _haystack = "\n".join(extract_text_content(m.get("content", "")) for m in messages)
            _blocks = await asyncio.to_thread(
                undelivered_for_chat, _owui_meta["owui_chat_id"], _haystack
            )
            _bridge_text = "\n\n".join(_blocks)
        except Exception as e:
            log.warning(f"Missions-Nachlieferung fehlgeschlagen: {e}")

    session = db.query(ChatSession).filter_by(user_id=current_user.id).order_by(ChatSession.created_at.desc()).first()
    if session:
        # Session-Rotation nach laengerer Pause. Vergleich in naive-UTC.
        _rotate_h = int(os.getenv("SESSION_ROTATE_HOURS", "6"))
        _last_msg = (db.query(Message).filter_by(session_id=session.id)
                     .order_by(Message.timestamp.desc()).first())
        _ref_ts = _last_msg.timestamp if _last_msg else session.created_at
        _now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if _rotate_h > 0 and _ref_ts and _ref_ts < _now_naive - timedelta(hours=_rotate_h):
            session = None
    if not session:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    if not _is_continue:
        # Beim Fortsetzen-Klick gibt es keine echte Nutzernachricht.
        db.add(Message(
            session_id=session.id,
            sender="user",
            # Bilder werden nicht persistiert, nur ein Marker.
            content_encrypted=encrypt_msg(
                ("[Bild angehaengt] " if user_images else "") + user_message_content
            ),
        ))
        db.commit()

    if _bridge_text:
        # Nachgelieferte Missions-Meldung sofort als eigener Assistant-Turn
        # persistieren -- unabhaengig vom Stream-Vertrag.
        db.add(Message(
            session_id=session.id,
            sender="assistant",
            content_encrypted=encrypt_msg(_bridge_text),
        ))
        db.commit()
        chat_history.append(AIMessage(content=_bridge_text))

    if _is_continue and _bridge_text:
        # Fortsetzen-Klick plus offene Missions-Meldung: deterministisch zustellen,
        # KEIN Agent-Lauf -- der koennte das Ergebnis nur paraphrasieren.
        _delivery = "\n\n" + _bridge_text
        if body.get("stream", False):
            async def stream_bridge():
                yield _sse_delta(_delivery)
                yield "data: [DONE]\n\n"
            return StreamingResponse(stream_bridge(), media_type="text/event-stream")
        return JSONResponse(_completion_json({"role": "assistant", "content": _delivery}))

    agent_payload = {"input": user_message_content, "chat_history": chat_history,
                     "user_id": current_user.id, "images": user_images, **_owui_meta}

    if body.get("stream", False):
        active_session_id = session.id
        async def stream_generator(session_id: int):
            final_answer_chunks = []
            _t0 = time.monotonic()
            _ttft_ms = None
            stream = agent_executor.astream(agent_payload)
            try:
                if _bridge_text:
                    # Missions-Nachlieferung VOR der Antwort streamen. Nicht in
                    # final_answer_chunks -- sie ist bereits persistiert.
                    yield _sse_delta(_bridge_text + "\n\n")
                async for chunk in stream:
                    if await request.is_disconnected():
                        log.info("Client disconnected — breche Stream ab.")
                        break
                    if not isinstance(chunk, dict):
                        continue
                    # Reasoning-Chunks als reasoning_content streamen, damit
                    # Open-WebUI den Denkblock rendert. Nicht in der Persistenz.
                    reasoning_chunk = chunk.get("reasoning")
                    if reasoning_chunk:
                        yield _sse_delta(reasoning=reasoning_chunk)
                        continue
                    #--- Freigabe-Anfrage: Open WebUI kann keine Karte zeigen, also
                    #    wird daraus eine Statuszeile.
                    conf_chunk = chunk.get("confirmation")
                    if conf_chunk:
                        _cphase = conf_chunk.get("phase")
                        if _cphase == "pending":
                            _cline = (f"⏳ Freigabe erforderlich ({conf_chunk.get('tool_name', '?')}) "
                                      "-- im Argus-Chat oder per Telegram bestaetigen...\n")
                        else:
                            _cmap = {"approved": "freigegeben", "rejected": "abgelehnt",
                                     "timeout": "abgelaufen"}
                            _cline = (f"⏳ Freigabe {_cmap.get(conf_chunk.get('status'), conf_chunk.get('status'))}.\n")
                        # Wie jede Statuszeile: gestreamt, aber nie persistiert.
                        final_answer_chunks.clear()
                        yield _sse_delta(_cline)
                        continue
                    status_chunk = chunk.get("status")
                    content_chunk = status_chunk or chunk.get("output")
                    if not content_chunk:
                        continue
                    if _ttft_ms is None:
                        _ttft_ms = (time.monotonic() - _t0) * 1000.0
                    # Statuszeilen werden gestreamt, aber nie persistiert.
                    if status_chunk:
                        final_answer_chunks.clear()
                    else:
                        final_answer_chunks.append(content_chunk)
                    yield _sse_delta(content_chunk)

                # --- Mission-Follow: der Stream bleibt offen und liefert Fortschritt
                # und Ergebnis live in die Bubble. Schichten: Follow -> Push -> Bruecke.
                # ACHTUNG: OWUI kappt Streams nach AIOHTTP_CLIENT_TIMEOUT Sekunden.
                if (_owui_meta.get("owui_chat_id")
                        and os.getenv("CLOUD_MISSION_FOLLOW", "true").lower() == "true"):
                    try:
                        from rag_backend.cloud_agents import missions as _missions
                        _follow_ids = await asyncio.to_thread(
                            _missions.active_for_chat, _owui_meta["owui_chat_id"]
                        )
                    except Exception as e:
                        log.warning(f"Mission-Follow: Start fehlgeschlagen: {e}")
                        _follow_ids = []
                    if _follow_ids:
                        # Registrierung VOR dem ersten Poll.
                        _missions._FOLLOWED.update(_follow_ids)
                        try:
                            _deadline = time.monotonic() + int(os.getenv("CLOUD_MISSION_FOLLOW_MAX_SECONDS", "240"))
                            _poll = max(2, int(os.getenv("CLOUD_MISSION_FOLLOW_POLL_SECONDS", "4")))
                            _pending = list(_follow_ids)
                            _last_step: dict = {}
                            log.info(f"Mission-Follow: begleite {_pending} im offenen Stream.")
                            while _pending and time.monotonic() < _deadline:
                                if await request.is_disconnected():
                                    log.info("Mission-Follow: Client weg -- Push/Bruecke uebernehmen.")
                                    break
                                # verhindert Reflection-Start mitten im Follow
                                request.app.state.last_activity = datetime.now(timezone.utc)
                                for _mid in list(_pending):
                                    _status, _step = await asyncio.to_thread(_missions.mission_step, _mid)
                                    if _status == "missing":
                                        _pending.remove(_mid)
                                        continue
                                    if _status in _missions._TERMINAL_NEEDLES:
                                        _text = await asyncio.to_thread(_missions.delivery_text, _mid)
                                        if _text:
                                            # Ergebnis streamen und persistieren;
                                            # die Bruecke stellt dann nichts doppelt zu.
                                            yield _sse_delta("\n\n" + _text)
                                            final_answer_chunks.append("\n\n" + _text)
                                        _pending.remove(_mid)
                                        _missions._FOLLOWED.discard(_mid)
                                        continue
                                    if _step and _last_step.get(_mid) != _step:
                                        _last_step[_mid] = _step
                                        # Fortschrittszeile: sichtbar, aber nicht
                                        # in der Persistenz.
                                        yield _sse_delta(f"\n⏳ Mission {_mid}: {_step} ...")
                                if _pending:
                                    await asyncio.sleep(_poll)
                            if _pending and not await request.is_disconnected():
                                _ids = ", ".join(str(i) for i in _pending)
                                yield _sse_delta(
                                    f"\n\nMission {_ids} läuft weiter im Hintergrund -- "
                                    "das Ergebnis erscheint automatisch hier im Chat."
                                )
                        except Exception as e:
                            # Follow ist Komfort -- ein Fehler darf den Turn nicht
                            # abreissen.
                            log.warning(f"Mission-Follow abgebrochen: {e}")
                        finally:
                            _missions._FOLLOWED.difference_update(_follow_ids)

                yield "data: [DONE]\n\n"
            finally:
                await stream.aclose()
                full_output = "".join(final_answer_chunks)
                total_ms = round((time.monotonic() - _t0) * 1000.0)
                ttft_val = round(_ttft_ms) if _ttft_ms is not None else None
                tokens_sec_val = None
                if total_ms and len(full_output):
                    secs = total_ms / 1000.0
                    if secs > 0:
                        tokens_sec_val = round((len(full_output) / metrics._CHARS_PER_TOKEN) / secs, 1)

                try:
                    metrics.record_request(
                        ttft_ms=ttft_val,
                        total_ms=total_ms,
                        completion_chars=len(full_output),
                    )
                except Exception:
                    pass
                cleaned = clean_agent_output(full_output)
                if cleaned:
                    try:
                        with SessionLocal() as db_stream:
                            db_stream.add(Message(
                                session_id=session_id,
                                sender="assistant",
                                content_encrypted=encrypt_msg(cleaned),
                                latency_ms=total_ms,
                                ttft_ms=ttft_val,
                                tokens_per_sec=tokens_sec_val
                            ))
                            db_stream.commit()
                    except Exception as e:
                        log.warning(f"Stream-Persistenz fehlgeschlagen: {e}")

        return StreamingResponse(stream_generator(active_session_id), media_type="text/event-stream")

    _t0 = time.monotonic()
    result = await agent_executor.ainvoke(agent_payload)
    total_ms = round((time.monotonic() - _t0) * 1000.0)
    cleaned_output = clean_agent_output(result.get("output"))
    tokens_sec_val = None
    if total_ms and cleaned_output:
        secs = total_ms / 1000.0
        if secs > 0:
            tokens_sec_val = round((len(cleaned_output) / metrics._CHARS_PER_TOKEN) / secs, 1)

    # Auch Non-Streaming-Antworten ins Dashboard zaehlen (nicht nur der Stream-Zweig).
    try:
        metrics.record_request(ttft_ms=None, total_ms=total_ms, completion_chars=len(cleaned_output or ""))
    except Exception:
        pass

    if cleaned_output:
        db.add(Message(
            session_id=session.id,
            sender="assistant",
            content_encrypted=encrypt_msg(cleaned_output),
            latency_ms=total_ms,
            ttft_ms=None,
            tokens_per_sec=tokens_sec_val
        ))
        db.commit()

    _assistant_msg = {
        "role": "assistant",
        # Nicht-Streaming: Missions-Nachlieferung der Antwort voranstellen.
        "content": (_bridge_text + "\n\n" + (cleaned_output or "")) if _bridge_text else cleaned_output,
    }
    _reasoning = result.get("reasoning") if isinstance(result, dict) else None
    if _reasoning:
        _assistant_msg["reasoning_content"] = _reasoning
    return JSONResponse(_completion_json(_assistant_msg))
"""
    writefile("rag_backend/main.py", MAIN_PY, "main", do_dedent=False)


    # ---------------------------------------------------------------------------
    # metrics.py  (Dashboard: In-Memory-Ringbuffer + SGLang-/metrics-Scrape)
    # ---------------------------------------------------------------------------
    METRICS_PY = r'''"""Leichtgewichtige Metrik-Sammlung fuers Ops-Dashboard.

Zwei Quellen:
- In-Process Ring-Buffer (record_*) fuer Agent-/Request-Metriken (Route, Tools,
  TTFT, Latenz, geschaetzte tok/s). Kein DB-Zugriff im Hot-Path.
- sglang_metrics(): scrapt SGLangs Prometheus-/metrics-Endpoint (braucht
  --enable-metrics am Server). Liefert die ECHTEN Engine-Zahlen (Decode-tok/s,
  laufende Requests, KV-Cache-Auslastung). Nicht erreichbar -> leeres Dict.
"""
import os
import re
import time
from collections import deque, Counter

import httpx

_MAX = int(os.getenv("DASHBOARD_METRICS_BUFFER", "200"))
_requests: deque = deque(maxlen=_MAX)
_route_counts: Counter = Counter()
_tool_counts: Counter = Counter()
_rag_scores: deque = deque(maxlen=_MAX)
#--- Kompaktierungen sind ein Gesamtzaehler, kein Ring-Buffer (siehe record_compaction).
_compactions_total: int = 0
_last_compaction: dict | None = None

# Char/Token-Schaetzung zentral in utils; als Modul-Attribut gespiegelt (main.py liest metrics._CHARS_PER_TOKEN).
from rag_backend.utils import CHARS_PER_TOKEN as _CHARS_PER_TOKEN


def record_request(*, ttft_ms=None, total_ms=None, completion_chars=0):
    tok_s = None
    if total_ms and completion_chars:
        secs = total_ms / 1000.0
        if secs > 0:
            tok_s = round((completion_chars / _CHARS_PER_TOKEN) / secs, 1)
    _requests.append({
        "ts": time.time(),
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "completion_chars": completion_chars,
        "tok_s": tok_s,
    })


def record_route(route):
    if route:
        _route_counts[route] += 1


def record_tool(name):
    if name:
        _tool_counts[name] += 1


def record_rag_score(score):
    # Top-Reranker-Relevanz pro document_search-Aufruf.
    try:
        _rag_scores.append(float(score))
    except (TypeError, ValueError):
        pass


def record_compaction(*, tokens_before=None, threshold=None, condensed=0):
    """Auto-Kompaktierung der Nachrichtenkette (agent.py) mitschreiben.

    Zaehlt bewusst NICHT ueber den Ring-Buffer: die Kompaktierung ist ein seltenes,
    erklaerungsbeduerftiges Ereignis -- 'seit wann laeuft der Stack, wie oft war der
    Kontext zu voll' soll auch nach 200 Requests noch stimmen. Am KV-Orb im Dashboard
    beantwortet das die Frage, ob eine niedrige KV-Auslastung echt ist oder gerade
    erst durch Verdichtung erkauft wurde."""
    global _compactions_total, _last_compaction
    _compactions_total += 1
    _last_compaction = {
        "ts": time.time(),
        "tokens_before": tokens_before,
        "threshold": threshold,
        "condensed": condensed,
    }


def snapshot():
    reqs = list(_requests)
    recent = reqs[-50:]

    def _avg(key):
        vals = [r[key] for r in recent if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    rag_recent = list(_rag_scores)[-50:]
    avg_rag_score = round(sum(rag_recent) / len(rag_recent), 2) if rag_recent else None

    return {
        "requests_total": len(reqs),
        "avg_tok_s_backend": _avg("tok_s"),
        "avg_ttft_ms": _avg("ttft_ms"),
        "avg_total_ms": _avg("total_ms"),
        "avg_rag_score": avg_rag_score,
        "routes": dict(_route_counts),
        "tools": dict(_tool_counts.most_common(12)),
        "recent": recent[-20:],
        "compactions_total": _compactions_total,
        "last_compaction": _last_compaction,
    }


_SGLANG_METRICS_URL = os.getenv("SGLANG_METRICS_URL", "http://sglang:30000/metrics")

# SGLang-Metriknamen koennen je Version abweichen -- bei Bedarf hier anpassen.
_WANTED = {
    "sglang:gen_throughput": "gen_throughput_tok_s",
    "sglang:num_running_reqs": "running_reqs",
    "sglang:num_queue_reqs": "queue_reqs",
    "sglang:token_usage": "kv_cache_usage",
    "sglang:cache_hit_rate": "cache_hit_rate",
}

_METRIC_RE = re.compile(r"^([a-zA-Z0-9_:]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$")


async def sglang_metrics():
    out = {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(_SGLANG_METRICS_URL)
            resp.raise_for_status()
            text = resp.text
    except Exception:
        return out
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, val = m.group(1), m.group(2)
        key = _WANTED.get(name)
        if key:
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out


_TTS_METRICS_URL = os.getenv("TTS_METRICS_URL", "http://tts-service:8002/v1/tts/metrics")

async def tts_metrics():
    out = {}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(_TTS_METRICS_URL)
            if resp.status_code == 200:
                out = resp.json()
    except Exception:
        pass
    return out


_SEARXNG_HEALTH_URL = os.getenv("SEARXNG_HEALTH_URL", "http://searxng:8080/")
_SEARXNG_TTL = float(os.getenv("SEARXNG_HEALTH_TTL", "15"))
_searxng_cache = {"ts": 0.0, "online": False}


async def searxng_online():
    # Echter Health-Check des SearxNG-Containers, gecacht (TTL).
    now = time.time()
    if now - _searxng_cache["ts"] < _SEARXNG_TTL:
        return _searxng_cache["online"]
    online = False
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(_SEARXNG_HEALTH_URL)
            online = resp.status_code < 500
    except Exception:
        online = False
    _searxng_cache["ts"] = now
    _searxng_cache["online"] = online
    return online


def load_historical_metrics(db_session):
    from datetime import timezone
    import logging

    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Message

    try:
        messages = (
            db_session.query(Message)
            .filter(Message.sender == "assistant")
            .filter(Message.latency_ms.isnot(None))
            .order_by(Message.timestamp.desc())
            .limit(100)
            .all()
        )
        _requests.clear()

        for msg in reversed(messages):
            try:
                decrypted = decrypt_msg(msg.content_encrypted)
                completion_chars = len(decrypted)
            except Exception:
                completion_chars = 0

            _requests.append({
                "ts": msg.timestamp.replace(tzinfo=timezone.utc).timestamp() if msg.timestamp else time.time(),
                "ttft_ms": msg.ttft_ms,
                "total_ms": msg.latency_ms,
                "completion_chars": completion_chars,
                "tok_s": msg.tokens_per_sec,
            })
        logging.getLogger(__name__).info(f"Loaded {len(messages)} historical metrics from the database.")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to load historical metrics: {e}")
'''
    writefile("rag_backend/metrics.py", METRICS_PY, "metrics", do_dedent=False)


    # ---------------------------------------------------------------------------
    # static/index.html (Ops-Dashboard) ist nach Setup_Dashboard.py ausgelagert
    # und wird am Ende dieses Laufs automatisch mitgeneriert.
    # ---------------------------------------------------------------------------



    # ---------------------------------------------------------------------------
    # fast_path.py
    # ---------------------------------------------------------------------------
    FAST_PATH_PY = textwrap.dedent("""\
        import os

        # Anrede aus OWNER_NAME (Setup2 -> .env). Leer = Kurzantworten ohne Namen.
        _OWNER = os.getenv("OWNER_NAME", "").strip()
        _ANREDE = f" {_OWNER}" if _OWNER else ""
        _KOMMA_ANREDE = f", {_OWNER}" if _OWNER else ""

        FAST_PATH_DICT = {
            # KEYS bleiben ASCII-transliteriert; die VALUES gehen woertlich an den
            # Nutzer -> echte Umlaute.
            "hallo":                f"Hallo{_ANREDE}! Wie kann ich dir heute helfen?",
            "hi":                   f"Hi{_ANREDE}! Was steht an?",
            "hey":                  "Hey! Was kann ich für dich tun?",
            "guten morgen":         "Guten Morgen! Bereit für den Tag?",
            "guten tag":            "Guten Tag! Wie kann ich behilflich sein?",
            "guten abend":          f"Guten Abend{_ANREDE}! Gibt es noch etwas zu klären?",
            "wer bist du":          "Ich bin Argus, dein vielseitiger persönlicher Assistent für Technik, Alltag und Recherche.",
            "was kannst du":        "Ich helfe dir bei Coding, Texten, komplexen Analysen oder alltäglichen Fragen -- und kann eigenständig im Web recherchieren.",
            "was kannst du tun":    "Ich helfe dir bei Coding, Texten, komplexen Analysen oder alltäglichen Fragen -- und kann eigenständig im Web recherchieren.",
            "hilfe":                "Frag mich einfach nach Problemen, lass mich Texte verfassen oder nutze meine Websuche für aktuelle Infos.",
            "help":                 "Just ask about problems, let me write texts or use my web search for current info.",
            "status":               "System läuft stabil. Alle Schnittstellen (SGLang, Qdrant, SearxNG, SQL) sind aktiv.",
            "version":              f"Argus RAG-System. Modell: {os.getenv('OAI_MODEL', 'unbekannt')}. Stack: SGLang + LangGraph + Qdrant + SearxNG.",
            "danke":                f"Gern geschehen{_KOMMA_ANREDE}!",
            "danke dir":            "Kein Problem!",
            "danke schoen":         "Immer gerne!",
            "vielen dank":          "Immer gerne!",
            "ok":                   "Alles klar.",
            "okay":                 "Verstanden.",
            "cool":                 "Freut mich!",
            "super":                "Sehr gut. Was kommt als Naechstes?",
            "perfekt":              "Sehr gut. Was kommt als Naechstes?",
            "gut gemacht":          "Danke! Weiter geht's.",
            "alles klar":           "Alles klar. Was als Naechstes?",
            "tschuess":             f"Bis bald{_KOMMA_ANREDE}!",
            "bye":                  f"Bis bald{_KOMMA_ANREDE}!",
            "ciao":                 "Schönen Tag noch!",
            "wer hat dich erschaffen": f"Ich bin dein persönliches KI-System, konfiguriert von dir{_KOMMA_ANREDE}.",
            "wie geht es dir":      "Ich bin bereit und voll einsatzfähig. Wobei darf ich dich unterstützen?",
        }
    """)
    writefile("rag_backend/fast_path.py", FAST_PATH_PY, "fast_path constants")


    # ---------------------------------------------------------------------------
    # ingest.py
    # ---------------------------------------------------------------------------
    INGEST_PY = textwrap.dedent(r"""
        import os
        import glob
        import uuid
        import time
        import hashlib
        import multiprocessing
        from datetime import datetime, timezone
        from pathlib import Path
        from typing import Optional

        import qdrant_client
        from bs4 import BeautifulSoup
        from sentence_transformers import SentenceTransformer
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from qdrant_client.http.models import PointStruct, Filter, FieldCondition, MatchValue
        import ebooklib
        from ebooklib import epub
        import docx
        import pypdfium2 as pdfium

        # Fallbacks spiegeln Setup2.LLM_CONFIG und muessen mit dem on-demand-Ingest
        # in agent.py uebereinstimmen.
        CHUNK_SIZE    = int(os.getenv("CHUNK_SIZE", 1536))
        CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", 192))
        COLLECTION    = os.getenv("QDRANT_COLLECTION", "docs")
        MODEL_NAME    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

        QDRANT_BATCH_SIZE = int(os.getenv("QDRANT_BATCH_SIZE", 256))
        NUM_INGESTION_WORKERS = int(os.getenv("NUM_INGESTION_WORKERS", min(12, os.cpu_count() or 1)))
        MAX_FILE_SIZE_MB = 100

        _QDRANT_CLIENT: Optional[qdrant_client.QdrantClient] = None
        _MODEL: Optional[SentenceTransformer] = None


        def _get_qdrant() -> qdrant_client.QdrantClient:
            global _QDRANT_CLIENT
            if _QDRANT_CLIENT is None:
                if os.getenv("QDRANT_IN_MEMORY", "false").lower() == "true":
                    print("  Qdrant wird im In-Memory-Modus initialisiert (Tests).")
                    _QDRANT_CLIENT = qdrant_client.QdrantClient(location=":memory:")
                else:
                    _QDRANT_CLIENT = qdrant_client.QdrantClient(url=os.getenv("QDRANT_URL", "http://qdrant:6333"))
            return _QDRANT_CLIENT


        def _get_model() -> SentenceTransformer:
            global _MODEL
            if _MODEL is None:
                _MODEL = SentenceTransformer(MODEL_NAME, device="cpu")
            return _MODEL


        def ensure_qdrant_collection(client, collection_name: str, dim: int) -> bool:
            # Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
            '''Legt die Collection samt Payload-Indizes an, wenn sie fehlt; True = neu erstellt.
            ZENTRAL fuer Batch-Ingest UND main.py-Startup (EINE Quelle der Erstellungslogik).'''
            try:
                client.get_collection(collection_name=collection_name)
                return False
            except Exception:
                pass
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_client.http.models.VectorParams(
                    size=dim,
                    distance=qdrant_client.http.models.Distance.COSINE,
                ),
            )
            for field, schema_type in [
                ("original_filename", qdrant_client.http.models.PayloadSchemaType.KEYWORD),
                ("source", qdrant_client.http.models.PayloadSchemaType.KEYWORD),
                ("chunk_index", qdrant_client.http.models.PayloadSchemaType.INTEGER),
                ("language", qdrant_client.http.models.PayloadSchemaType.KEYWORD),
                ("created_at", qdrant_client.http.models.PayloadSchemaType.DATETIME),
                ("success_score", qdrant_client.http.models.PayloadSchemaType.FLOAT),
            ]:
                try:
                    client.create_payload_index(
                        collection_name=collection_name,
                        field_name=field,
                        field_schema=schema_type,
                    )
                except Exception as index_err:
                    print(f"  WARNUNG: Payload-Index fuer '{field}' nicht erstellt: {index_err}")
            return True


        def build_splitter(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
            '''Einheitlicher Text-Splitter fuer Batch-Ingest UND Tool-Ingest (agent.py
            _store_text_in_qdrant) -- beide chunken dieselbe Collection und muessen
            identisch arbeiten, sonst driftet das Chunking je nach Ingest-Pfad.'''
            return RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                length_function=len,
                separators=["\n\n", "\n", ". ", "! ", "? ", "; ", ": ", ", ", " ", ""],
                is_separator_regex=False,
            )


        from rag_backend.database import SessionLocal


        def _compute_hash(filepath: Path) -> str:
            h = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()


        def _get_known_hash(filename: str) -> Optional[str]:
            try:
                from rag_backend.models import IngestedDocument
                with SessionLocal() as db:
                    record = db.query(IngestedDocument).filter_by(filename=filename).first()
                    return record.file_hash if record else None
            except Exception:
                return None


        def _save_document_record(filename: str, file_path: str, file_hash: str, chunk_count: int):
            try:
                from rag_backend.models import IngestedDocument
                with SessionLocal() as db:
                    record = db.query(IngestedDocument).filter_by(filename=filename).first()
                    if record:
                        record.file_path = file_path
                        record.file_hash = file_hash
                        record.chunk_count = chunk_count
                        record.ingested_at = datetime.now(timezone.utc)
                    else:
                        db.add(IngestedDocument(
                            filename=filename,
                            file_path=file_path,
                            file_hash=file_hash,
                            chunk_count=chunk_count,
                        ))
                    db.commit()
            except Exception as e:
                print(f"  -> Warnung: DB-Tracking fehlgeschlagen fuer {filename}: {e}")


        def _delete_document_record(filename: str):
            try:
                from rag_backend.models import IngestedDocument
                with SessionLocal() as db:
                    db.query(IngestedDocument).filter_by(filename=filename).delete()
                    db.commit()
            except Exception as e:
                print(f"  -> Warnung: DB-Loeschung fehlgeschlagen fuer {filename}: {e}")


        def _delete_qdrant_chunks(filename: str):
            try:
                _get_qdrant().delete(
                    collection_name=COLLECTION,
                    points_selector=Filter(
                        must=[FieldCondition(
                            key="original_filename",
                            match=MatchValue(value=filename),
                        )]
                    ),
                )
            except Exception as e:
                print(f"  -> Warnung: Qdrant-Loeschung fehlgeschlagen fuer {filename}: {e}")


        def load_file(filepath: Path) -> Optional[str]:
            try:
                if not filepath.is_file():
                    print(f"  -> Warnung: {filepath.name} ist kein File. Ueberspringe.")
                    return None
                if filepath.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    print(f"  -> Ueberspringe {filepath.name}: > {MAX_FILE_SIZE_MB} MB.")
                    return None

                suffix = filepath.suffix.lower()
                text_content = None

                if suffix == ".pdf":
                    try:
                        doc = pdfium.PdfDocument(str(filepath))
                        parts = []
                        for i in range(len(doc)):
                            page = doc[i]
                            tp = page.get_textpage()
                            parts.append(tp.get_text_range())
                            tp.close()
                            page.close()
                        doc.close()
                        text_content = "\n".join(parts)
                    except Exception:
                        import pdfplumber
                        with pdfplumber.open(filepath) as pdf:
                            text_content = "\n".join(page.extract_text() or "" for page in pdf.pages)

                elif suffix == ".docx":
                    text_content = "\n".join(p.text for p in docx.Document(filepath).paragraphs)

                elif suffix in {".html", ".htm"}:
                    soup = BeautifulSoup(filepath.read_text("utf-8", errors="ignore"), "html.parser")
                    text_content = soup.get_text(separator="\n")

                elif suffix == ".epub":
                    book_text = []
                    try:
                        book = epub.read_epub(filepath)
                        for item in book.get_items():
                            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                                soup = BeautifulSoup(item.get_body_content(), "html.parser")
                                book_text.append(soup.get_text(separator="\n"))
                    except Exception as e:
                        print(f"  -> EPUB-Fehler {filepath.name}: {e}")
                        return None
                    text_content = "\n\n".join(book_text)

                elif suffix == ".pptx":
                    from pptx import Presentation
                    folien = []
                    for nr, folie in enumerate(Presentation(str(filepath)).slides, 1):
                        teile = [sh.text for sh in folie.shapes
                                 if getattr(sh, "has_text_frame", False) and sh.text.strip()]
                        if teile:
                            folien.append(f"--- Folie {nr} ---\n" + "\n".join(teile))
                    text_content = "\n\n".join(folien)

                elif suffix == ".xlsx":
                    import openpyxl
                    # read_only spart Speicher bei grossen Mappen, data_only
                    # liefert berechnete Werte statt Formeltexten.
                    wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
                    blaetter = []
                    for ws in wb.worksheets:
                        zeilen = []
                        for row in ws.iter_rows(values_only=True):
                            zellen = [str(c) for c in row if c is not None]
                            if zellen:
                                zeilen.append("\t".join(zellen))
                        if zeilen:
                            blaetter.append(f"--- Blatt: {ws.title} ---\n" + "\n".join(zeilen))
                    wb.close()
                    text_content = "\n\n".join(blaetter)

                # .env/.log bewusst NICHT in der Allow-List: solche Dateien enthalten
                # haeufig Secrets.
                elif suffix in {".txt", ".md", ".json", ".yaml", ".yml", ".csv",
                                ".py", ".ini", ".cfg"}:
                    text_content = filepath.read_text("utf-8", errors="ignore")

                else:
                    print(f"  -> Unbekannter Dateityp uebersprungen: {filepath.name}")
                    return None

                kb = filepath.stat().st_size / 1024
                if text_content is None:
                    # Leeres Ergebnis abfangen, sonst wirft len(None) einen TypeError.
                    print(f"  -> Kein Textinhalt extrahiert: {filepath.name}")
                    return None
                print(f"  -> Geladen: {filepath.name} ({kb:.1f} KB, {len(text_content)} Zeichen)")
                return text_content

            except Exception as e:
                print(f"  -> Fehler beim Laden von {filepath.name}: {e}")
                return None


        def _process_single_file_for_ingestion(args):
            f_path_str, db_filename, chunk_size, chunk_overlap = args
            _path = Path(f_path_str)
            text = load_file(_path)
            if not text or not text.strip():
                return [], [], 0, 1, 0
            splitter = build_splitter(chunk_size, chunk_overlap)
            chunks = splitter.split_text(text)
            metadata = [
                {"source": f_path_str, "original_filename": db_filename, "chunk_index": i}
                for i, _ in enumerate(chunks)
            ]
            return chunks, metadata, len(text), 0, 1


        def upsert_in_batches(client, collection_name, points, batch_size):
            for i in range(0, len(points), batch_size):
                client.upsert(collection_name=collection_name, points=points[i:i + batch_size], wait=True)


        def ingest(data_path="documents_to_ingest"):
            start_time = time.time()
            model = _get_model()
            print(f"\n--- Starte Ingestion fuer Pfad: '{data_path}' ---")

            if ensure_qdrant_collection(_get_qdrant(), COLLECTION, model.get_sentence_embedding_dimension()):
                print(f"  Collection '{COLLECTION}' nicht gefunden -- neu erstellt.")
            else:
                print(f"  Collection '{COLLECTION}' in Qdrant gefunden.")

            Path(data_path).mkdir(exist_ok=True)
            files = [f for f in glob.glob(f"{data_path}/**/*", recursive=True) if Path(f).is_file()]
            if not files:
                print(f"  Keine Dateien in '{data_path}'. Abbruch.")
                return
            print(f"  {len(files)} Dateien gefunden.")

            def _db_filename_for(filepath: Path) -> str:
                # DB-/Qdrant-Schluessel wie beim Ordner-Ingest: Relativpfad mit
                # '_'-Separatoren. Der nackte Dateiname kollidierte bei gleichnamigen
                # Dateien in verschiedenen Unterordnern.
                try:
                    rel = filepath.relative_to(data_path)
                except ValueError:
                    rel = Path(filepath.name)
                return str(rel).replace("/", "_").replace("\\", "_")

            files_to_process = []
            skipped_unchanged = 0
            for f in files:
                filepath = Path(f)
                db_filename = _db_filename_for(filepath)
                current_hash = _compute_hash(filepath)
                known_hash = _get_known_hash(db_filename)
                if known_hash and known_hash == current_hash:
                    print(f"  -> Unveraendert: {db_filename} -- uebersprungen.")
                    skipped_unchanged += 1
                else:
                    if known_hash:
                        print(f"  -> Geaendert: {db_filename} -- wird neu indexiert.")
                    files_to_process.append((f, db_filename, current_hash))

            if not files_to_process:
                print(f"  Alle {skipped_unchanged} Dateien unveraendert. Nichts zu tun.")
                return

            print(f"  {len(files_to_process)} Datei(en) werden verarbeitet, {skipped_unchanged} unveraendert.")

            task_args = [(f, db_filename, CHUNK_SIZE, CHUNK_OVERLAP) for f, db_filename, _ in files_to_process]
            print(f"  Starte parallele Verarbeitung mit {NUM_INGESTION_WORKERS} Worker(n)...")
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(NUM_INGESTION_WORKERS) as pool:
                results = pool.map(_process_single_file_for_ingestion, task_args)

            total_chars = 0
            skipped_empty = 0
            processed = 0

            for (f_path_str, db_filename, file_hash), (chunks, meta, chars, s, p) in zip(files_to_process, results):
                filepath = Path(f_path_str)
                skipped_empty += s
                if not chunks:
                    continue

                print(f"  -> Delete-before-Upsert: {db_filename} ({len(chunks)} Chunks)...")
                _delete_qdrant_chunks(db_filename)
                if db_filename != filepath.name:
                    # Einmalige Migration: Altbestand lief unter dem nackten Namen.
                    _delete_qdrant_chunks(filepath.name)
                    _delete_document_record(filepath.name)

                print(f"  -> Generiere Embeddings fuer {db_filename}...")
                vectors = model.encode(chunks, show_progress_bar=False)
                points = [
                    PointStruct(id=str(uuid.uuid4()), vector=v.tolist(), payload={**m, "text": c})
                    for v, c, m in zip(vectors, chunks, meta)
                ]
                upsert_in_batches(_get_qdrant(), COLLECTION, points, QDRANT_BATCH_SIZE)
                _save_document_record(db_filename, f_path_str, file_hash, len(chunks))

                total_chars += chars
                processed += 1

            dur = time.time() - start_time
            print(f"\n--- Ingestion Summary: {processed} neu indexiert, "
                  f"{skipped_unchanged} unveraendert, {skipped_empty} leer/fehler, "
                  f"{total_chars} Zeichen, {dur:.1f}s ---")


        def list_indexed_documents():
            try:
                from rag_backend.models import IngestedDocument
                with SessionLocal() as db:
                    records = db.query(IngestedDocument).order_by(IngestedDocument.ingested_at.desc()).all()
                    if not records:
                        print("  Keine Dokumente indexiert.")
                        return
                    print(f"  {'Dateiname':<40} {'Chunks':>6}  {'Indexiert am'}")
                    print(f"  {'-'*40} {'-'*6}  {'-'*20}")
                    for r in records:
                        ts = r.ingested_at.strftime('%Y-%m-%d %H:%M') if r.ingested_at else 'unbekannt'
                        print(f"  {r.filename:<40} {r.chunk_count or 0:>6}  {ts}")
            except Exception as e:
                print(f"  Fehler beim Abrufen der Dokumente: {e}")


        def delete_document(filename: str):
            print(f"  Loesche '{filename}' aus Qdrant und DB...")
            _delete_qdrant_chunks(filename)
            _delete_document_record(filename)
            print(f"  '{filename}' erfolgreich geloescht.")


        if __name__ == "__main__":
            import sys
            if len(sys.argv) > 1:
                if sys.argv[1] == "list":
                    list_indexed_documents()
                elif sys.argv[1] == "delete" and len(sys.argv) > 2:
                    delete_document(sys.argv[2])
                else:
                    print("Verwendung: python ingest.py [list | delete <dateiname>]")
            else:
                ingest()
    """)
    writefile("rag_backend/ingest.py", INGEST_PY, "ingestion script")


    # ---------------------------------------------------------------------------
    # Tests
    # ---------------------------------------------------------------------------
    TEST_MAIN_PY = textwrap.dedent("""\
        import os

        #--- Test-Umgebung VOR jedem rag_backend-Import setzen. Steht sie darunter,
        #--- hat der Import crypto_utils bereits mit dem dann geltenden Fernet-Key
        #--- initialisiert.
        os.environ.setdefault("SKIP_MIGRATIONS", "true")
        os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
        #--- 32+ Zeichen: darunter warnt PyJWT bei jedem encode/decode (HS256).
        os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
        os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
        os.environ.setdefault("OAI_MODEL", "test-model")

        import pytest
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from rag_backend.models import Base
        from rag_backend.main import app, get_db
        from rag_backend.ingest import ingest
        from pathlib import Path
        import time
        from unittest.mock import patch, MagicMock
        import asyncio

        SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"
        engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Lifespan neutralisieren: der echte Startup laeuft gegen die
        # postgres-gebundene SessionLocal, die der Host nicht aufloesen kann.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _noop_lifespan(_app):
            yield

        app.router.lifespan_context = _noop_lifespan

        #--- Rate-Limits in den Tests aus: alle Anfragen kommen von derselben
        #--- Adresse und wuerden sich gegenseitig aussperren.
        from rag_backend.main import limiter as _limiter

        _limiter.enabled = False

        @pytest.fixture(name="db_session", scope="function")
        def override_get_db():
            Base.metadata.create_all(bind=engine)
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()
                Base.metadata.drop_all(bind=engine)
                engine.dispose()
                db_file = Path("test.db")
                if db_file.exists():
                    db_file.unlink()

        @pytest.fixture(name="client", scope="function")
        def test_client(db_session):
            app.dependency_overrides[get_db] = lambda: db_session
            #--- base_url ist PFLICHT wegen des Host-Header-Gates: TestClient
            #--- schickt sonst 'Host: testserver' und bekommt 400. 'testserver'
            #--- gehoert NICHT in TRUSTED_HOSTS.
            with TestClient(app, base_url="http://127.0.0.1:7860") as client:
                yield client
            app.dependency_overrides.clear()

        @pytest.fixture(name="ingest_in_memory", scope="session")
        def ingest_in_memory_fixture():
            collection_name = "test_collection_" + str(time.time()).replace(".", "")
            os.environ["QDRANT_IN_MEMORY"] = "true"
            os.environ["QDRANT_COLLECTION"] = collection_name
            test_docs_dir = Path("test_documents_to_ingest")
            test_docs_dir.mkdir(exist_ok=True)
            (test_docs_dir / "test.txt").write_text("Dies ist ein Test.")
            ingest(data_path=str(test_docs_dir))
            yield collection_name
            (test_docs_dir / "test.txt").unlink()
            test_docs_dir.rmdir()
            del os.environ["QDRANT_IN_MEMORY"]
            del os.environ["QDRANT_COLLECTION"]

        def test_health(client):
            response = client.get("/healthz")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

        #--- Dashboard-Zugang: Anmeldung, Rollen, Upload-Grenzen ---------------------
        #--- Das Netz unter der Rollentrennung: faellt an einer Route das Gate weg,
        #--- faellt es hier auf.

        DASH_ORIGIN = {"Origin": "http://127.0.0.1:7860"}

        #--- Wer was sehen darf. Bewusst redundant zu den Depends() in main.py.
        ADMIN_ONLY_GET = [
            "/v1/dashboard/summary", "/v1/dashboard/audit", "/v1/dashboard/state",
            "/v1/dashboard/documents", "/v1/dashboard/missions", "/v1/dashboard/evals",
            "/v1/dashboard/eval_logs", "/v1/dashboard/uploads",
            "/v1/dashboard/users", "/v1/dashboard/confirmations",
        ]
        ANY_ACCOUNT_GET = ["/v1/dashboard/config", "/v1/dashboard/chats"]


        def _make_account(client, username, password, role):
            '''Legt ein Konto an und liefert einen angemeldeten Client-Cookie-Satz.'''
            from rag_backend.main import _login_accounts
            from rag_backend.database import get_db as _unused  # noqa: F401
            r = client.get("/v1/dashboard/auth/state")
            if r.json().get("needs_setup"):
                r = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                                json={"username": username, "password": password, "role": "admin"})
                assert r.status_code == 200, r.text
                return r
            r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                            json={"username": username, "password": password})
            assert r.status_code == 200, r.text
            return r


        def test_dashboard_requires_login(client):
            for path in ADMIN_ONLY_GET + ANY_ACCOUNT_GET:
                assert client.get(path).status_code == 401, path


        def test_first_account_becomes_admin(client):
            r = client.get("/v1/dashboard/auth/state")
            assert r.json()["needs_setup"] is True
            r = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                            json={"username": "chef", "password": "einLangesPasswort", "role": "admin"})
            assert r.status_code == 200
            assert r.json()["role"] == "admin"
            # Zweiter Erstlauf muss zu sein, sonst koennte sich jeder zum Admin machen.
            r2 = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN,
                             json={"username": "zweiter", "password": "einLangesPasswort", "role": "admin"})
            assert r2.status_code == 409


        def test_self_registration_never_yields_admin(client, monkeypatch):
            '''Der Kern der Selbstregistrierung: die Rolle steht serverseitig fest.

            Geprueft wird auch der Versuch, sie ueber den Body zu setzen -- genau daran
            scheitert der alte /register-Endpunkt, dessen Spalten-Default "admin" ist.'''
            _make_account(client, "chef", "einLangesPasswort", "admin")
            client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)

            r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                            json={"username": "neuling", "password": "einLangesPasswort"})
            assert r.status_code == 200, r.text
            assert r.json()["role"] == "chat"

            # Rolle im Body wird ignoriert, nicht uebernommen.
            r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                            json={"username": "schlaufuchs", "password": "einLangesPasswort",
                                  "role": "admin"})
            assert r.status_code == 200
            assert r.json()["role"] == "chat"

            # Und das frisch registrierte Konto kommt an keine Betriebsdaten.
            for path in ADMIN_ONLY_GET:
                assert client.get(path).status_code == 403, path


        def test_self_registration_requires_first_admin(client):
            # Vor dem ersten Konto fuehrt der Weg ueber /auth/setup.
            assert client.get("/v1/dashboard/auth/state").json()["needs_setup"] is True
            r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                            json={"username": "voreilig", "password": "einLangesPasswort"})
            assert r.status_code == 409


        def test_self_registration_can_be_switched_off(client, monkeypatch):
            _make_account(client, "chef", "einLangesPasswort", "admin")
            client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
            monkeypatch.setenv("DASHBOARD_ALLOW_REGISTER", "false")
            assert client.get("/v1/dashboard/auth/state").json()["allow_register"] is False
            r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                            json={"username": "zuspaet", "password": "einLangesPasswort"})
            assert r.status_code == 403


        def test_chat_role_is_locked_out_of_operations(client):
            _make_account(client, "chef", "einLangesPasswort", "admin")
            r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                            json={"username": "phoenix", "password": "einLangesPasswort", "role": "chat"})
            assert r.status_code == 200 and r.json()["role"] == "chat"

            client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
            r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                            json={"username": "phoenix", "password": "einLangesPasswort"})
            assert r.status_code == 200 and r.json()["role"] == "chat"

            for path in ADMIN_ONLY_GET:
                assert client.get(path).status_code == 403, path
            for path in ANY_ACCOUNT_GET:
                assert client.get(path).status_code == 200, path
            # Ein 'chat'-Konto darf sich auch nicht selbst befoerdern.
            r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                            json={"username": "hintertuer", "password": "einLangesPasswort", "role": "admin"})
            assert r.status_code == 403


        def test_chats_are_separated_per_account(client):
            _make_account(client, "chef", "einLangesPasswort", "admin")
            chat_id = client.post("/v1/dashboard/chats", headers=DASH_ORIGIN).json()["id"]
            client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                        json={"username": "phoenix", "password": "einLangesPasswort", "role": "chat"})
            client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
            client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                        json={"username": "phoenix", "password": "einLangesPasswort"})
            # Fremder Verlauf: 404 statt 403, damit die Existenz nicht durchsickert.
            assert client.get("/v1/dashboard/chats/" + str(chat_id)).status_code == 404
            assert client.delete("/v1/dashboard/chats/" + str(chat_id),
                                 headers=DASH_ORIGIN).status_code == 404
            assert client.get("/v1/dashboard/chats").json()["chats"] == []


        def test_dashboard_csrf_blocks_foreign_origin(client):
            _make_account(client, "chef", "einLangesPasswort", "admin")
            assert client.post("/v1/dashboard/chats",
                               headers={"Origin": "http://boese.example"}).status_code == 403
            # Ohne Origin/Referer: kein Browser-Kontext, also abgelehnt.
            assert client.post("/v1/dashboard/chats").status_code == 403


        def test_last_admin_cannot_be_demoted(client):
            r = _make_account(client, "chef", "einLangesPasswort", "admin")
            uid = r.json()["user_id"]
            r = client.post("/v1/dashboard/users/" + str(uid) + "/role?role=chat",
                            headers=DASH_ORIGIN)
            assert r.status_code == 409


        def test_upload_name_is_sanitized():
            from rag_backend.main import _safe_upload_name
            import pytest as _pytest
            from fastapi import HTTPException

            assert _safe_upload_name("../../../etc/passwd.txt") == "passwd.txt"
            assert _safe_upload_name("..\\\\..\\\\windows\\\\notiz.txt") == "notiz.txt"
            # Doppelpunkt = alternativer NTFS-Datenstrom, muss entschaerft werden.
            assert ":" not in _safe_upload_name("bericht.txt:geheim")
            for evil in ["", "..", "...", "/", "\\\\"]:
                with _pytest.raises(HTTPException):
                    _safe_upload_name(evil)


        def test_upload_path_stays_in_workspace():
            from rag_backend.main import _upload_path, _UPLOAD_DIR
            import pytest as _pytest
            from fastapi import HTTPException

            assert _upload_path("bericht.pdf").parent == _UPLOAD_DIR.resolve(strict=False)
            for evil in ["../../run/secrets/ssh_key", "..\\\\..\\\\secrets"]:
                # Entweder der Name wird flachgeklopft oder die Pruefung schlaegt zu
                # -- ein Pfad AUSSERHALB darf nie entstehen.
                try:
                    p = _upload_path(evil)
                except HTTPException:
                    continue
                assert p.parent == _UPLOAD_DIR.resolve(strict=False), evil

        def test_ingest_all_nimmt_nur_lesbare_dateien(client, tmp_path, monkeypatch):
            #--- Bilder liegen im selben Ordner, tragen aber keinen Text. Sie
            #--- duerfen den Sammellauf weder abbrechen noch als Fehler auftauchen.
            from rag_backend import main as m
            import rag_backend.agent as ag

            _make_account(client, "chef", "einLangesPasswort", "admin")

            ordner = tmp_path / "Uploads"
            ordner.mkdir()
            (ordner / "notiz.txt").write_text("Inhalt fuer den Index.", encoding="utf-8")
            (ordner / "bild.png").write_bytes(b"kein Text")
            monkeypatch.setattr(m, "_UPLOAD_DIR", ordner)

            gesehen = []

            async def _fake_store(name, text, source=None):
                gesehen.append(name)
                return "OK"

            monkeypatch.setattr(ag, "_store_text_in_qdrant", _fake_store)

            r = client.post("/v1/dashboard/uploads/ingest_all", headers=DASH_ORIGIN)
            assert r.status_code == 200, r.text
            assert r.json()["verarbeitet"] == 1
            assert r.json()["indexiert"] == 1
            assert gesehen == ["notiz.txt"]


        def test_ingest_all_braucht_admin_und_csrf(client, tmp_path, monkeypatch):
            from rag_backend import main as m

            #--- Ohne Anmeldung: kein Zugang.
            assert client.post("/v1/dashboard/uploads/ingest_all").status_code in (401, 403)

            _make_account(client, "chef", "einLangesPasswort", "admin")
            ordner = tmp_path / "Uploads"
            ordner.mkdir()
            monkeypatch.setattr(m, "_UPLOAD_DIR", ordner)
            #--- Angemeldet, aber ohne Origin: kein Browser-Kontext -> CSRF-Sperre.
            assert client.post("/v1/dashboard/uploads/ingest_all").status_code == 403
            r = client.post("/v1/dashboard/uploads/ingest_all", headers=DASH_ORIGIN)
            assert r.status_code == 200 and r.json()["verarbeitet"] == 0


        @pytest.mark.asyncio
        @patch("rag_backend.agent._qdrant_client")
        @patch("rag_backend.agent._reranker")
        @patch("rag_backend.agent._embedding_model")
        async def test_document_search_xml_format(mock_embedding, mock_reranker, mock_qdrant):
            from rag_backend.agent import document_search

            mock_embedding.encode.return_value = [0.1, 0.2, 0.3]

            mock_hit = MagicMock()
            mock_hit.payload = {"text": "Testinhalt aus Qdrant.", "original_filename": "test_doc.txt"}
            
            mock_query_response = MagicMock()
            mock_query_response.points = [mock_hit]
            mock_qdrant.query_points.return_value = mock_query_response

            mock_reranker.predict.return_value = [0.95]

            result = await document_search.ainvoke({"query": "Test"})

            assert "<document index=\\"1\\"" in result
            assert "source=\\"test_doc.txt\\"" in result
            assert "relevance=\\"0.95\\"" in result
            assert "Testinhalt aus Qdrant." in result

        def test_sanitize_tool_args_strips_sglang_leak():
            # SGLang laesst bei grammar-constrained Decoding das schliessende '>'
            # von '</parameter>' weg -> das Fragment leakt ans Wert-Ende.
            from rag_backend.agent import _sanitize_tool_args
            leaked = {
                'goal': 'Marktanalyse zu KI-Agenten\\n</parameter',
                'context': 'fuer eine Praesentation\\n</parameter',
                'provider': 'gemini\\n</parameter',
            }
            clean = _sanitize_tool_args(leaked)
            assert clean['goal'] == 'Marktanalyse zu KI-Agenten'
            assert clean['context'] == 'fuer eine Praesentation'
            assert clean['provider'] == 'gemini'
            # Gestapelte Fragmente (letzter Parameter vor </function></tool_call>)
            assert _sanitize_tool_args({'q': 'x\\n</parameter\\n</function\\n</tool_call'})['q'] == 'x'
            # KEINE Beschaedigung legitimer Werte
            assert _sanitize_tool_args({'a': 'gemini'})['a'] == 'gemini'
            assert _sanitize_tool_args({'a': '3 < 5 ist wahr'})['a'] == '3 < 5 ist wahr'
            assert _sanitize_tool_args({'a': 'a<b und x>y'})['a'] == 'a<b und x>y'
            # Geschlossenes Tag mitten im Text bleibt unberuehrt
            assert _sanitize_tool_args({'a': 'Nutze </parameter> als Beispiel hier'})['a'] == 'Nutze </parameter> als Beispiel hier'
            # Rekursiv in verschachtelten Strukturen
            nested = _sanitize_tool_args({'outer': {'inner': 'v\\n</parameter'}, 'lst': ['a\\n</parameter', 'b']})
            assert nested['outer']['inner'] == 'v'
            assert nested['lst'] == ['a', 'b']
            # Zweite Leak-Variante (10.07.): '</parameter >' MIT Leerzeichen vor '>'
            assert _sanitize_tool_args({'q': 'wert\\n</parameter >'})['q'] == 'wert'
            # Leak plus fehlserialisierte Liste -> JSON-Koerzion repariert den Typ.
            coerced = _sanitize_tool_args({'subtopics': '["a", "b"]\\n</parameter >'})
            assert coerced['subtopics'] == ['a', 'b']
            # OHNE Leak bleibt ein JSON-artiger String bewusst String (execute_code!)
            assert _sanitize_tool_args({'code': '[1, 2, 3]'})['code'] == '[1, 2, 3]'
            # Leak + ungueltiges JSON -> gestrippter String, keine Koerzion
            assert _sanitize_tool_args({'a': '[PERSON_1]\\n</parameter'})['a'] == '[PERSON_1]'

        def test_sanitize_via_aimessage_patch():
            # Der Monkeypatch muss den Leak bereits bei AIMessage-Konstruktion entfernen.
            from langchain_core.messages import AIMessage
            msg = AIMessage(content='', tool_calls=[{
                'name': 'start_mission',
                'args': {'goal': 'Recherche\\n</parameter', 'provider': 'gemini\\n</parameter'},
                'id': 'call_1', 'type': 'tool_call',
            }])
            assert msg.tool_calls[0]['args']['goal'] == 'Recherche'
            assert msg.tool_calls[0]['args']['provider'] == 'gemini'

        def test_tool_call_rescue_from_reasoning():
            # Qwen3.5 legt den Folge-Call gelegentlich komplett in den Reasoning-
            # Kanal -- der Normalizer promotet ihn zu einem echten tool_call.
            from langchain_core.messages import AIMessage
            rc = ('Die Teilagenten lieferten nichts. Ich suche direkt.\\n'
                  '<tool_call>\\n<function=web_search>\\n<parameter=query>\\n'
                  'Patch Tuesday Juli 2026 kritische CVEs\\n</parameter>\\n</function>\\n</tool_call>')
            msg = AIMessage(content='', additional_kwargs={'reasoning_content': rc})
            assert msg.tool_calls and msg.tool_calls[0]['name'] == 'web_search'
            assert msg.tool_calls[0]['args']['query'] == 'Patch Tuesday Juli 2026 kritische CVEs'
            # Hat content dagegen echten Antworttext, bleiben Reasoning-Calls Gedanken.
            msg2 = AIMessage(content='Hier ist die fertige Antwort.',
                             additional_kwargs={'reasoning_content': rc})
            assert not msg2.tool_calls

        def test_strip_status_lines():
            from rag_backend.main import strip_status_lines
            text = (
                "Ich recherchiere mal, wann Google mit einem eigenen Multimodal-Modell nachzieht.\\n"
                "\\n"
                "🔍 Suche im Web: \\"Google multimodal model launch\\"...\\n"
                "📄 Lese Seite: https://blog.google/technology/ai/...\\n"
                "Hier ist das echte Ergebnis.\\n"
                "Es funktioniert prima!\\n"
                "⏳ Mission 1: checking...\\n"
                "🔧 calculate(expression=1+1)...\\n"
                "Ende der Nachricht."
            )
            expected = (
                "Ich recherchiere mal, wann Google mit einem eigenen Multimodal-Modell nachzieht.\\n"
                "\\n"
                "Hier ist das echte Ergebnis.\\n"
                "Es funktioniert prima!\\n"
                "Ende der Nachricht."
            )
            assert strip_status_lines(text) == expected

        def test_strip_status_lines_codepoint_variants():
            # Regression fuer byte-genaue Emoji-Mismatches: Labels ohne Variation
            # Selector und die Blackboard-Node-Zeilen muessen alle gestrippt werden.
            from rag_backend.main import strip_status_lines
            text = (
                "Echte Antwort davor.\\n"
                "\\U0001F550 Rufe aktuelle Uhrzeit ab...\\n"
                "\\U0001F324 Rufe Wetter ab: Bonn...\\n"
                "\\U0001F6E1 Pruefe Windows Updates...\\n"
                "\\U0001F6E1\\uFE0F Variante MIT Variation Selector...\\n"
                "\\U0001F5D1 Loesche Dokument: alt.pdf...\\n"
                "\\U0001F9F5 Starte 3 Recherche-Agenten (Wellen von 3)...\\n"
                "\\U0001F9E0 Solver-Agent analysiert und arbeitet an Loesung (Schritt 1)...\\n"
                "\\U0001F4DD Synthetisiere Endergebnis...\\n"
                "Echte Antwort danach."
            )
            assert strip_status_lines(text) == "Echte Antwort davor.\\nEchte Antwort danach."
    """)
    writefile("rag_backend/tests/test_main.py", TEST_MAIN_PY, "backend tests")
    Path("rag_backend/tests/__init__.py").touch()
    Path("rag_backend/__init__.py").touch()


    # ---------------------------------------------------------------------------
    # pytest.ini (README wird handgepflegt, nicht generiert)
    # ---------------------------------------------------------------------------
    writefile("pytest.ini", textwrap.dedent("""\
        [pytest]
        asyncio_mode = auto
    """), "pytest.ini")

    # Dashboard (static/index.html) lebt in Setup_Dashboard.py -- automatisch
    # mitgenerieren, damit ein Clean-Rebuild komplett bleibt.
    if (Path(__file__).parent / "Setup_Dashboard.py").exists():
        import Setup_Dashboard
        Setup_Dashboard.main()
    else:
        _log("WARNUNG: Setup_Dashboard.py fehlt -- static/index.html wird nicht generiert (/dashboard/ liefert 404).")

    verify_markers(BASE_DIR)

    _log("")
    _log("Setup1 abgeschlossen.")
    _log("Naechster Schritt: python Setup2.py")
