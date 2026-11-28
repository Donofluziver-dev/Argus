#!/usr/bin/env python3
"""Richtet eine neue Argus-Installation ein: Geheimnisse und .env.

    python setup/init_stack.py [--workspace <Ordner>] [--models <Ordner>]

Legt an, was fehlt, und laesst alles Vorhandene unveraendert. Ein zweiter Lauf
ist harmlos. Ausgegeben werden nur Dateinamen, nie Werte.

  API_Tokens/   Fernet-Schluessel, Modell-Schluessel, JWT, Audit-HMAC,
                Postgres-Passwort, leere Platzhalter fuer optionale Schluessel
  .env          aus .env.example, mit SEARXNG_SECRET, CANARY_TOKENS und dem
                Einrichtungs-Schluessel fuer das erste Konto (DASHBOARD_SETUP_TOKEN)
  Koeder-Datei  zugangsdaten.txt im Arbeitsordner (siehe README, CANARY_TOKENS)

Braucht Python ab 3.10 und das Paket "cryptography":
    python -m pip install cryptography
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

FERNET_KEY_FILE = "fernet_key.txt"
# llama.cpp, Backend und Sprachdienste nutzen denselben Schluessel.
LLM_API_KEY_FILE = "llm_api_key.txt"
# Name aus der SGLang-Zeit; wird beim naechsten Lauf umbenannt.
LEGACY_LLM_API_KEY_FILE = "sglang_api_key.txt"
ENC_PREFIX = "ENC:"

# (Datei, verschluesselt, Zufallsbytes). postgres_password bleibt Klartext:
# Postgres liest die Datei direkt.
GENERATED = [
    (LLM_API_KEY_FILE, True, 32),
    ("jwt_secret.txt", True, 48),
    ("audit_hmac_key.txt", True, 32),
    ("postgres_password.txt", False, 24),
]
# Optionale Schluessel des Nutzers. Leer angelegt, damit Docker die Einbindung
# nicht als Ordner anlegt. Einen LangSmith-Schluessel gibt es nicht mehr: Ablaufdaten
# gehen nur an Phoenix im eigenen Haus, das Backend laedt ihn nicht.
OPTIONAL_PLACEHOLDERS = ["Gemini.txt"]

# Unter Linux gelten die Dateirechte auch im Container:
# - llm, embed und rerank laufen als root OHNE Capabilities (cap_drop: ALL, also
#   ohne DAC_OVERRIDE) und duerfen eine fremde 0600-Datei nicht lesen.
# - rag-backend, stt-service und tts-service laufen unter einer unprivilegierten UID.
# Deshalb bekommt der Ordner API_Tokens/ unter Nicht-Windows 0700 (haelt andere
# lokale Nutzer auf dem Host bereits am Verzeichnis fern) und die Dateien darin
# 0644 (Docker bindet die einzelnen Dateien ein; im Container sind sie dann fuer
# jede UID lesbar).
SECRET_DIR_MODE_POSIX = 0o700
SECRET_FILE_MODE_POSIX = 0o644

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def random_token(nbytes: int = 32) -> str:
    """Zufallswert aus [A-Za-z0-9_-]: sicher in Shell, HTTP-Headern und Datenbank-URLs."""
    return secrets.token_urlsafe(nbytes)


def is_valid_fernet_key(key: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(key.encode("ascii"))) == 32
    except Exception:
        return False


def _read_trimmed(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def _write_atomic(path: Path, content: str, mode: int = 0o600) -> None:
    """Erst Hilfsdatei, dann umbenennen. Ohne BOM, ohne Zeilenende."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if os.name != "nt":
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def restrict_to_owner(directory: Path) -> bool:
    """Windows: nur Administratoren, SYSTEM und der aktuelle Nutzer, ohne Vererbung
    von oben. Gruppen ueber SIDs, damit es auf jedem Sprach-Windows greift.
    Andere Systeme: Ordner auf 0700 setzen."""
    if os.name != "nt":
        try:
            os.chmod(directory, SECRET_DIR_MODE_POSIX)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME", "")
    if os.environ.get("USERDOMAIN"):
        user = f"{os.environ['USERDOMAIN']}\\{user}"
    if not user.strip("\\"):
        return False
    icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    try:
        subprocess.run(
            [str(icacls), str(directory), "/inheritance:r", "/grant:r",
             "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", f"{user}:(OI)(CI)F"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def ensure_stack_secrets(directory: Path, restrict_acl=restrict_to_owner) -> tuple[dict[str, str], bool]:
    """Legt fehlende Geheimnisse an. Liefert (Datei -> was passiert ist, Rechte gesetzt)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SystemExit(
            'Das Paket "cryptography" fehlt. Installieren mit:\n'
            "    python -m pip install cryptography\n"
            "Unter Linux mit gesperrtem System-Python in einer eigenen Umgebung:\n"
            "    python3 -m venv .venv && .venv/bin/pip install cryptography && "
            ".venv/bin/python setup/init_stack.py"
        ) from exc

    directory.mkdir(parents=True, exist_ok=True)
    acl_applied = bool(restrict_acl(directory))
    if os.name != "nt":
        os.chmod(directory, SECRET_DIR_MODE_POSIX)
        acl_applied = True

    file_mode = 0o600 if os.name == "nt" else SECRET_FILE_MODE_POSIX
    files: dict[str, str] = {}

    key_path = directory / FERNET_KEY_FILE
    key = _read_trimmed(key_path)
    if key:
        if not is_valid_fernet_key(key):
            raise SystemExit(
                f"{FERNET_KEY_FILE} ist kein gueltiger Fernet-Schluessel. Datei pruefen statt "
                "sie zu ersetzen: verschluesselte Geheimnisse und Chats haengen an ihr.")
        files[FERNET_KEY_FILE] = "behalten"
    else:
        key = Fernet.generate_key().decode("ascii")
        _write_atomic(key_path, key, mode=file_mode)
        files[FERNET_KEY_FILE] = "angelegt"
    fernet = Fernet(key.encode("ascii"))

    def seal(value: str, encrypt: bool) -> str:
        return ENC_PREFIX + fernet.encrypt(value.encode("utf-8")).decode("ascii") if encrypt else value

    # Aeltere Installation: den Schluessel behalten, nur den Dateinamen umstellen.
    legacy = directory / LEGACY_LLM_API_KEY_FILE
    if not (directory / LLM_API_KEY_FILE).exists() and _read_trimmed(legacy):
        os.replace(legacy, directory / LLM_API_KEY_FILE)
        files[LEGACY_LLM_API_KEY_FILE] = "umbenannt"

    for name, encrypt, nbytes in GENERATED:
        path = directory / name
        if _read_trimmed(path):
            files[name] = "behalten"
            continue
        _write_atomic(path, seal(random_token(nbytes), encrypt), mode=file_mode)
        files[name] = "angelegt"

    for name in OPTIONAL_PLACEHOLDERS:
        path = directory / name
        if path.exists():
            files[name] = "behalten"
        else:
            _write_atomic(path, "", mode=file_mode)
            files[name] = "Platzhalter"

    # Bei einem zweiten Lauf unter Nicht-Windows auch die Rechte bereits vorhandener
    # Dateien in API_Tokens/ auf 0644 korrigieren.
    if os.name != "nt":
        for entry in directory.iterdir():
            if entry.is_file():
                os.chmod(entry, SECRET_FILE_MODE_POSIX)

    return files, acl_applied


def resolve_linux_ugid(euid: int | None = None, egid: int | None = None) -> tuple[int, int]:
    """Ermittelt (ARGUS_UID, ARGUS_GID) unter Linux.
    Laeuft die Installation als root (UID 0), gilt der Nutzer hinter sudo (SUDO_UID,
    SUDO_GID); ohne sudo 1000:1000. 1000 kann auf einem Server jemand anderem gehoeren."""
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 1000
    if egid is None:
        egid = os.getegid() if hasattr(os, "getegid") else 1000
    if euid == 0:
        sudo_uid = (os.environ.get("SUDO_UID") or "").strip()
        sudo_gid = (os.environ.get("SUDO_GID") or "").strip()
        if sudo_uid.isdigit() and int(sudo_uid) > 0:
            return int(sudo_uid), int(sudo_gid) if sudo_gid.isdigit() else int(sudo_uid)
        return 1000, 1000
    return int(euid), int(egid)


def ensure_runtime_dirs(
    workspace: Path | None = None,
    evals: Path | None = None,
    models: Path | None = None,
    *,
    extra: list[Path] | tuple[Path, ...] = (),
    euid: int | None = None,
    egid: int | None = None,
) -> list[Path]:
    """Legt Arbeitsordner (samt Uploads/), Evals-Ordner, Modellordner und weitere
    Ordner (extra, z. B. proxy/data) an, falls sie fehlen. Laeuft die Installation
    unter Linux als root (UID 0), bekommen die Ordner den Besitzer 1000:1000."""
    if euid is None and os.name != "nt" and hasattr(os, "geteuid"):
        euid = os.geteuid()
    if egid is None and os.name != "nt" and hasattr(os, "getegid"):
        egid = os.getegid()
    chown_target = resolve_linux_ugid(euid, egid) if (os.name != "nt" and euid == 0) else None

    targets: list[Path] = []
    if workspace is not None:
        targets.append(workspace)
        targets.append(workspace / "Uploads")
    if evals is not None:
        targets.append(evals)
    if models is not None:
        targets.append(models)
    targets.extend(extra)

    for d in targets:
        d.mkdir(parents=True, exist_ok=True)
        if chown_target is not None and hasattr(os, "chown"):
            os.chown(d, chown_target[0], chown_target[1])
    return targets


def parse_env(text: str) -> dict[str, str]:
    """Schluessel -> Wert. Anfuehrungszeichen werden entfernt, wie compose es tut."""
    out: dict[str, str] = {}
    for raw in re.split(r"\r?\n", text):
        m = _ENV_LINE.match(raw.strip())
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def format_env_value(value: str) -> str:
    """Unkritisches bleibt ohne Anfuehrungszeichen; sonst einfache, die compose
    woertlich nimmt. Zeilenumbrueche und Werte mit ' samt Sonderzeichen lassen sich
    nicht sicher abbilden."""
    if re.search(r"[\r\n\0]", value):
        raise ValueError("Zeilenumbruch in einem .env-Wert.")
    if re.fullmatch(r"[A-Za-z0-9_.,:/@+=-]*", value):
        return value
    if "'" in value:
        raise ValueError("Wert mit ' und Sonderzeichen kann die .env nicht sicher abbilden.")
    return f"'{value}'"


def set_env_values(text: str, values: dict[str, str]) -> str:
    """Setzt Werte in einem .env-Text. Vorhandene Schluessel werden an ihrer Stelle
    ersetzt (auch doppelte), fehlende am Ende angefuegt. Kommentare, Reihenfolge und
    Zeilenenden bleiben erhalten."""
    for k in values:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise ValueError(f"Ungueltiger Schluessel: {k}")
    eol = "\r\n" if "\r\n" in text else "\n"
    seen: set[str] = set()
    # Trenner mitnehmen: jede Zeile behaelt ihr eigenes Zeilenende.
    parts = re.split(r"(\r?\n)", text)
    for i in range(0, len(parts), 2):
        m = _ENV_LINE.match(parts[i].strip())
        if not m or m.group(1) not in values:
            continue
        seen.add(m.group(1))
        parts[i] = f"{m.group(1)}={format_env_value(values[m.group(1)])}"
    out = "".join(parts)
    missing = [k for k in values if k not in seen]
    if missing:
        out = re.sub(r"(\r?\n)+$", "", out)
        if out:
            out += eol
        out += eol.join(f"{k}={format_env_value(values[k])}" for k in missing) + eol
    return out


def write_env_file(env_path: Path, template_text: str, values: dict[str, str]) -> bool:
    """Erzeugt die .env aus der Vorlage, wenn sie fehlt, und setzt die Werte.
    Ohne Aenderung kein Schreiben. Die .env bleibt immer 0600."""
    exists = env_path.exists()
    base = env_path.read_bytes().decode("utf-8") if exists else template_text
    new = set_env_values(base, values)
    if exists and new == base:
        if os.name != "nt":
            os.chmod(env_path, 0o600)
        return False
    tmp = env_path.with_name(f"{env_path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    return True


def generate_env_secrets() -> dict[str, str]:
    """Geheimnisse, die in der .env stehen statt in API_Tokens/."""
    return {
        "SEARXNG_SECRET": secrets.token_hex(32),
        # Nur wer ihn kennt, legt im Browser das erste Administrator-Konto an.
        "DASHBOARD_SETUP_TOKEN": random_token(18),
        # Sieht aus wie ein echter Schluessel, damit ein Abfluss auffaellt.
        "CANARY_TOKENS": f"sk-argus-{random_token(24)}",
        # Qdrant nimmt nur Anfragen mit diesem Schluessel an (Backend und Container).
        "QDRANT_API_KEY": random_token(32),
        # Backend und Code-Sandbox; ohne ihn nimmt die Sandbox keinen Lauf an.
        "SANDBOX_TOKEN": random_token(32),
    }


def write_canary_bait(workspace: Path, token: str) -> bool:
    """Koeder-Datei im Arbeitsordner. Wird nur angelegt, wenn sie fehlt."""
    path = workspace / "zugangsdaten.txt"
    if path.exists():
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, f"# Zugang Buchhaltungs-API\nAPI_KEY={token}\n")
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and hasattr(os, "chown"):
        uid, gid = resolve_linux_ugid(0, 0)
        os.chown(workspace, uid, gid)
        os.chown(path, uid, gid)
    return True


def _usable_here(path_text: str) -> bool:
    """Die Vorlage nennt Windows-Pfade. Unter Linux wuerde "C:/..." als Ordner im
    aktuellen Verzeichnis angelegt."""
    return os.name == "nt" or not re.match(r"^[A-Za-z]:[\\/]", path_text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Geheimnisse und .env einer Argus-Installation anlegen.")
    ap.add_argument("--stack", default=str(Path(__file__).resolve().parent.parent),
                    help="Ordner der Installation (Standard: dieses Repo)")
    ap.add_argument("--workspace", help="Arbeitsordner (ARGUS_WORKSPACE_DIR)")
    ap.add_argument("--models", help="Ordner der Modelle (ARGUS_MODELS_DIR)")
    # Die gefuehrte Installation (setup/install.py) erledigt die naechsten Schritte selbst.
    ap.add_argument("--ohne-naechste-schritte", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    stack = Path(args.stack).resolve()
    template_path, env_path = stack / ".env.example", stack / ".env"
    if not (stack / "docker-compose.yml").exists() or not template_path.exists():
        print(f"Fehler: {stack} ist kein Argus-Ordner (docker-compose.yml und .env.example fehlen).",
              file=sys.stderr)
        return 1

    files, acl_applied = ensure_stack_secrets(stack / "API_Tokens")
    print("Geheimnisse in API_Tokens/:")
    for name, action in files.items():
        print(f"  {action:<11} {name}")
    if os.name == "nt" and not acl_applied:
        print("  Hinweis: Die Rechte auf den Ordner konnten nicht gesetzt werden.")
    if os.name != "nt":
        print("  Rechte gesetzt: Ordner 0700 (nur Besitzer), Dateien 0644 (im Container lesbar).")

    # Nur leere Werte fuellen: eine bestehende .env behaelt ihre Geheimnisse.
    template = template_path.read_bytes().decode("utf-8")
    current = parse_env(env_path.read_bytes().decode("utf-8") if env_path.exists() else template)
    values = {k: v for k, v in generate_env_secrets().items() if not current.get(k)}
    # Bestehende Installationen bekommen beim Update die bisherigen Dienste als
    # Profile eingetragen, falls der Schluessel noch fehlt. Vor Version 2 gab es
    # das Profil web noch nicht und SearXNG lief immer: Bei alten Ständen (< 2)
    # wird web an vorhandene Profile angehaengt und die Version auf 2 gesetzt.
    # Ist die Version schon mindestens 2, bleibt der Profil-Zustand unberuehrt.
    env_version_raw = current.get("ARGUS_ENV_VERSION")
    try:
        env_version = int(env_version_raw) if env_version_raw is not None else 0
    except ValueError:
        env_version = 0

    if "COMPOSE_PROFILES" not in current:
        values["COMPOSE_PROFILES"] = "voice,tracing,web"
        values["ARGUS_ENV_VERSION"] = "2"
    elif env_version < 2:
        prof_list = [p.strip() for p in current["COMPOSE_PROFILES"].split(",") if p.strip()]
        if "web" not in prof_list:
            prof_list.append("web")
        values["COMPOSE_PROFILES"] = ",".join(prof_list)
        values["ARGUS_ENV_VERSION"] = "2"
    #--- Bildeingabe: Wer das Standardmodell nutzt und den Schluessel noch gar nicht
    #--- hat, bekommt dessen Zusatzdatei (mmproj) eingetragen. Ein bewusst leerer
    #--- Eintrag bleibt leer; zu einem eigenen Modell passt die Datei nicht.
    template_env = parse_env(template)
    if ("LLM_MMPROJ_FILE" not in current and template_env.get("LLM_MMPROJ_FILE")
            and current.get("LLM_MODEL_FILE") == template_env.get("LLM_MODEL_FILE")):
        values["LLM_MMPROJ_FILE"] = template_env["LLM_MMPROJ_FILE"]
    if "LLM_MMPROJ_OFFLOAD" not in current and "LLM_MMPROJ_OFFLOAD" in template_env:
        values["LLM_MMPROJ_OFFLOAD"] = template_env["LLM_MMPROJ_OFFLOAD"]
    if os.name != "nt":
        uid, gid = resolve_linux_ugid()
        if not current.get("ARGUS_UID"):
            values["ARGUS_UID"] = str(uid)
        if not current.get("ARGUS_GID"):
            values["ARGUS_GID"] = str(gid)

    if args.workspace:
        ws_dir = args.workspace.replace("\\", "/").rstrip("/")
        values["ARGUS_WORKSPACE_DIR"] = ws_dir
        # Der Evals-Ordner liegt in der Vorlage im Arbeitsordner und zieht mit um,
        # solange ihn niemand eigens gesetzt hat.
        if current.get("ARGUS_EVALS_DIR", "") in ("", parse_env(template).get("ARGUS_EVALS_DIR", "")):
            values["ARGUS_EVALS_DIR"] = f"{ws_dir}/Evals"
    if args.models:
        values["ARGUS_MODELS_DIR"] = args.models.replace("\\", "/")

    created = not env_path.exists()
    written = write_env_file(env_path, template, values)
    what = "aus .env.example angelegt" if created else ("ergaenzt" if written else "unveraendert")
    print(f"\n.env {what}; gesetzt: {', '.join(values) or 'nichts'}")

    env_now = parse_env(env_path.read_bytes().decode("utf-8"))
    for key, flag in (("ARGUS_WORKSPACE_DIR", "--workspace"), ("ARGUS_MODELS_DIR", "--models"),
                      ("ARGUS_EVALS_DIR", "--workspace")):
        if not _usable_here(env_now.get(key, "")):
            print(f"Hinweis: {key} steht noch auf einem Windows-Pfad. Bitte in der .env anpassen "
                  f"oder mit {flag} setzen.")

    ws_str = env_now.get("ARGUS_WORKSPACE_DIR", "")
    evals_str = env_now.get("ARGUS_EVALS_DIR", "")
    models_str = env_now.get("ARGUS_MODELS_DIR", "")
    ws_path = Path(ws_str) if (ws_str and _usable_here(ws_str) and (args.workspace or os.name != "nt")) else None
    evals_path = Path(evals_str) if (evals_str and _usable_here(evals_str) and (args.workspace or os.name != "nt")) else None
    models_path = Path(models_str) if (models_str and _usable_here(models_str) and (args.models or os.name != "nt")) else None
    ensure_runtime_dirs(workspace=ws_path, evals=evals_path, models=models_path)

    if "CANARY_TOKENS" in values:
        ws = env_now.get("ARGUS_WORKSPACE_DIR", "")
        if ws and _usable_here(ws):
            if write_canary_bait(Path(ws), values["CANARY_TOKENS"]):
                print(f"Koeder-Datei angelegt: {Path(ws) / 'zugangsdaten.txt'}")
        else:
            print("Hinweis: Koeder-Datei nicht angelegt, weil der Arbeitsordner noch nicht feststeht.\n"
                  "  Danach von Hand: zugangsdaten.txt im Arbeitsordner mit dem Wert von CANARY_TOKENS.")

    if args.ohne_naechste_schritte:
        return 0
    print(f"""
Naechste Schritte:
  1. .env pruefen (Modell passend zur Grafikkarte).
  2. Modelle nach ARGUS_MODELS_DIR legen (siehe README).
  3. In {stack}: docker compose up -d --build
  4. Im Browser http://127.0.0.1:7860 oeffnen und das erste Administrator-Konto anlegen.
     Die Seite fragt dafuer nach dem Einrichtungs-Schluessel: Er steht in der .env
     unter DASHBOARD_SETUP_TOKEN.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
