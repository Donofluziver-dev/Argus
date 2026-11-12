#!/usr/bin/env python3
"""Argus MCP-Filesystem-Server.

Path-Whitelist: C:\\Argus_Workspace (Container-Mount: /host/argus_workspace).
Tools: fs_read_file, fs_write_file, fs_list_directory, fs_mkdir.
fs_delete bewusst weggelassen: Loeschen gibt es ueber diese Werkzeuge nicht.
"""

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

#--- stdout gehoert dem MCP-Protokoll: die SIEM-Ausgabe (AUDIT_SIEM=stdout) weicht
#--- in diesem Prozess auf stderr aus.
os.environ["ARGUS_STDOUT_RESERVED"] = "1"

WORKSPACE_CONTAINER = Path("/host/argus_workspace")
WORKSPACE_WIN = "C:\\Argus_Workspace"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mcp.fs] %(levelname)s %(message)s",
)
log = logging.getLogger("mcp.fs")

mcp = FastMCP("argus-filesystem")


def _to_container(path_str: str) -> Path:
    """Konvertiert Windows-Pfad oder Container-Pfad zu Container-Pfad."""
    s = path_str.strip().rstrip("/\\")
    if s.lower().startswith(WORKSPACE_WIN.lower()):
        rel = s[len(WORKSPACE_WIN):].lstrip("\\/").replace("\\", "/")
        return WORKSPACE_CONTAINER / rel if rel else WORKSPACE_CONTAINER
    if s.startswith(str(WORKSPACE_CONTAINER)):
        return Path(s)
    #--- Absoluter Pfad ausserhalb des Workspace: klar abweisen statt als
    #--- workspace-relativ umzudeuten -- das erzeugte eine irrefuehrende Meldung.
    _is_drive = len(s) >= 2 and s[1] == ":" and s[0].isalpha()
    if _is_drive or s.startswith("/"):
        raise PermissionError(
            f"Path '{path_str}' is outside the allowed workspace ({WORKSPACE_WIN}). "
            f"The fs_* tools only see the workspace."
        )
    #--- Relative Pfade als Workspace-Relativ interpretieren
    return WORKSPACE_CONTAINER / s.lstrip("/\\")


def _is_user_isolated_path(target: Path, workspace_real: Path) -> bool:
    """Prueft, ob ein Pfad innerhalb von users/<id>/Dateien oder users/<id>/Ergebnisse liegt.
    Dateien und Ergebnisse sind isolierte Benutzerbereiche und fuer fs-Tools tabu.
    Gedaechtnis (users/<id>/MEMORY.md und users/<id>/memory/) bleibt zulaessig."""
    try:
        parts = target.resolve(strict=False).relative_to(workspace_real).parts
        if len(parts) >= 3 and parts[0].lower() == "users" and parts[2].lower() in ("dateien", "ergebnisse"):
            return True
    except (ValueError, RuntimeError, OSError):
        pass
    try:
        parts_orig = target.relative_to(workspace_real).parts
        if len(parts_orig) >= 3 and parts_orig[0].lower() == "users" and parts_orig[2].lower() in ("dateien", "ergebnisse"):
            return True
    except (ValueError, RuntimeError, OSError):
        pass
    return False


def _enforce_whitelist(path: Path) -> Path:
    """Resolvt Path und prueft dass er unter WORKSPACE_CONTAINER bleibt.

    realpath schuetzt gegen Symlink-Escape und ..-Traversal. Wir erlauben
    auch noch-nicht-existierende Pfade (fuer write/mkdir), pruefen dann
    das Eltern-Verzeichnis.

    PARITAET: agent.py::_win_to_container nutzt dieselbe resolve+relative_to-
    Whitelist (andere Fehlersemantik: None statt Exception). Aenderungen an der
    Pruef-Logik IMMER an beiden Stellen nachziehen.
    """
    try:
        real = path.resolve(strict=False)
    except (RuntimeError, OSError) as e:
        raise PermissionError(f"Path not resolvable: {path} ({e})")
    workspace_real = WORKSPACE_CONTAINER.resolve(strict=False)
    try:
        real.relative_to(workspace_real)
    except ValueError:
        raise PermissionError(
            f"Path outside workspace whitelist ({WORKSPACE_WIN}): {path}"
        )
    if _is_user_isolated_path(path, workspace_real) or _is_user_isolated_path(real, workspace_real):
        raise PermissionError(
            f"Access denied: '{path}' is inside isolated user storage (Dateien/Ergebnisse)."
        )
    return real


#--- identity/: Prompt-Dateien. Evals/: Skripte, die im Backend-Container (mit
#--- SSH-Schluessel und allen Secrets) ausgefuehrt werden, plus die Audit-Kopien.
#--- Vergleich OHNE Gross-/Kleinschreibung: der Workspace ist ein Windows-Mount,
#--- 'EVALS/x.py' landet dort in 'Evals/x.py'.
_READONLY_SUBDIRS = ("identity", "evals")
#--- Dateien im Workspace-Wurzelordner, die Sicherheitszustand halten (z. B. der
#--- SSH-Host-Key-Pin .argus_known_hosts).
_READONLY_ROOT_PREFIXES = (".argus",)


def _enforce_writable(real: Path) -> Path:
    """Verbietet Schreibzugriff auf Prompt-/Identitaetsdateien (identity/*). Diese werden in
    den System-Prompt geladen (get_prompt_from_env); waeren sie agent-schreibbar, koennte ein
    per Prompt-Injection gesteuerter Agent seinen eigenen System-Prompt umschreiben (M1/ASI06).
    Ebenso Evals/* (ausgefuehrte Skripte, Audit-Kopien) und .argus*-Dateien im Wurzelordner.
    users/* bleibt bewusst schreibbar (Agent-gepflegtes Gedaechtnis/Reflexion)."""
    workspace_real = WORKSPACE_CONTAINER.resolve(strict=False)
    try:
        parts = real.relative_to(workspace_real).parts
    except ValueError:
        raise PermissionError(f"Path outside workspace whitelist ({WORKSPACE_WIN}): {real}")
    if parts and parts[0].lower() in _READONLY_SUBDIRS:
        raise PermissionError(
            f"Write access denied: '{parts[0]}/' is read-only (prompt files, evaluation "
            "scripts and audit copies); read access only."
        )
    if len(parts) == 1 and parts[0].lower().startswith(_READONLY_ROOT_PREFIXES):
        raise PermissionError(
            f"Write access denied: '{parts[0]}' holds security state and is read-only."
        )
    return real


def _to_windows(real_path: Path) -> str:
    """Konvertiert Container-Pfad zurueck zu Windows-Pfad fuer Output."""
    workspace_real = WORKSPACE_CONTAINER.resolve(strict=False)
    try:
        rel = real_path.relative_to(workspace_real)
    except ValueError:
        return str(real_path)
    rel_str = str(rel).replace("/", "\\")
    if rel_str in (".", ""):
        return WORKSPACE_WIN
    return f"{WORKSPACE_WIN}\\{rel_str}"


def _audit(op: str, path: str, content: Optional[str] = None) -> None:
    content_hash = ""
    if content is not None:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        log.info("op=%s path=%s bytes=%d hash=%s", op, path, len(content), content_hash)
    else:
        log.info("op=%s path=%s", op, path)


def _central_audit(tool: str, op: str, path: str, content: Optional[str] = None) -> None:
    # Workspace-Mutationen zusaetzlich ins HMAC-signierte AuditLog. Fehlertolerant:
    # darf die fs-Operation nie brechen.
    try:
        from rag_backend.action_engine.audit import write_audit_log
        in_h = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        out_h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16] if content is not None else None
        write_audit_log(
            tool=tool, tool_class="write", input_hash=in_h, output_hash=out_h,
            status="ok", details=f"{op}: {path}",
        )
    except Exception as e:
        log.warning("zentrales AuditLog fuer fs-Op fehlgeschlagen: %s", e)


@mcp.tool()
def fs_read_file(path: str) -> str:
    """Reads a file in the Argus workspace. Path as a Windows path (e.g. C:\\Argus_Workspace\\Temp\\foo.py) or relative to the workspace.

    Returns: complete file content as a string. Raises PermissionError if the
    path is outside the whitelist. Raises FileNotFoundError if the file does
    not exist.
    """
    p = _enforce_whitelist(_to_container(path))
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    content = p.read_text(encoding="utf-8")
    # Output-Cap wie bei run_powershell (20k).
    _max = int(os.getenv("FS_READ_MAX_CHARS", "20000"))
    if len(content) > _max:
        content = content[:_max] + f"\n... [file truncated after {_max} chars -- read on selectively]"
    _audit("read", _to_windows(p))
    if content:
        try:
            from rag_backend.egress_guard import mark_sensitive_source
            mark_sensitive_source("fs_read_file", content)
        except Exception:
            pass
    return content


@mcp.tool()
def fs_write_file(path: str, content: str) -> str:
    """Writes a file in the Argus workspace. Overwrites if it exists -- creating a backup as <file>.bak.<UTC timestamp> in the same folder first. Parent directories are created as needed.

    Returns: confirmation text with path, character count and backup path if any.
    """
    from datetime import datetime, timezone
    p = _enforce_whitelist(_to_container(path))
    _enforce_writable(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    backup_info = ""
    if p.is_file():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = p.with_name(p.name + f".bak.{ts}")
        shutil.copy2(p, backup)
        backup_info = f" (Backup: {_to_windows(backup)})"
        _audit("backup", _to_windows(backup))
    p.write_text(content, encoding="utf-8")
    _audit("write", _to_windows(p), content)
    _central_audit("fs_write_file", "write", _to_windows(p), content)
    return f"OK written: {_to_windows(p)} ({len(content)} chars){backup_info}"


@mcp.tool()
def fs_list_directory(path: str = "C:\\Argus_Workspace", recursive: bool = False) -> str:
    """Lists files and subdirectories in the Argus workspace. Default path is the workspace root.

    Args:
        path: directory (Windows path or relative).
        recursive: if True, list all files in subfolders recursively.

    Returns: newline-separated list of paths (directories with a trailing backslash).
    """
    p = _enforce_whitelist(_to_container(path))
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    workspace_real = WORKSPACE_CONTAINER.resolve(strict=False)
    items = []
    if recursive:
        for f in sorted(p.rglob("*")):
            if _is_user_isolated_path(f, workspace_real):
                continue
            suffix = "\\" if f.is_dir() else ""
            items.append(f"{_to_windows(f)}{suffix}")
    else:
        for f in sorted(p.iterdir()):
            if _is_user_isolated_path(f, workspace_real):
                continue
            suffix = "\\" if f.is_dir() else ""
            items.append(f"{_to_windows(f)}{suffix}")
    _audit("list", _to_windows(p))
    if items:
        try:
            from rag_backend.egress_guard import mark_sensitive_source
            mark_sensitive_source("fs_list_directory", "\n".join(items))
        except Exception:
            pass
    return "\n".join(items) if items else "(empty)"


@mcp.tool()
def fs_mkdir(path: str) -> str:
    """Creates a directory in the Argus workspace (including parents, idempotent).

    Returns: confirmation text with path.
    """
    p = _enforce_whitelist(_to_container(path))
    _enforce_writable(p)
    p.mkdir(parents=True, exist_ok=True)
    _audit("mkdir", _to_windows(p))
    _central_audit("fs_mkdir", "mkdir", _to_windows(p), None)
    return f"OK created: {_to_windows(p)}"


def main():
    if not WORKSPACE_CONTAINER.exists():
        log.warning("Workspace-Mount %s nicht vorhanden -- docker-compose Bind-Mount pruefen.", WORKSPACE_CONTAINER)
    else:
        log.info("Workspace-Whitelist: %s -> %s", WORKSPACE_WIN, WORKSPACE_CONTAINER)
    mcp.run()


if __name__ == "__main__":
    main()
