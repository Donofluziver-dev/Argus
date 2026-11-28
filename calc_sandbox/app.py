import asyncio
import base64
import ctypes
import hmac
import os
import re as _re
import resource
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import warnings
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import sympy
from sympy import E, pi, I, oo
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations, implicit_multiplication_application,
)

app = FastAPI(title="Calc Sandbox")

# Dienst laeuft im Container als root (uid 0) mit cap_drop: ALL und nur
# SETUID, SETGID, KILL. Ausgefuehrter Code laeuft unter "runner" (uid/gid 10001),
# Sympy-Rechnungen laufen unter "calc" (uid/gid 10002), jeweils ohne Capabilities.
_RUNNER_UID = 10001
_RUNNER_GID = 10001
_CALC_UID = 10002
_CALC_GID = 10002
_REQUIRED_CAPS_MASK = (1 << 5) | (1 << 6) | (1 << 7)  # KILL (5), SETGID (6), SETUID (7)
_MISCONFIG_MSG = "Sandbox falsch gestartet: Dienst muss als root mit SETUID/SETGID/KILL laufen"
_KILL_MAX_ATTEMPTS = 25
_CALC_TIMEOUT_SEC = 5.0
_CALC_CPU_LIMIT_SEC = (6, 7)
_CALC_MEM_LIMIT_BYTES = 256 * 1024 * 1024

_SAFE_SANDBOX_NAME_RE = _re.compile(r"[^\w._ ()\-]")
_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


#--- Paritaet mit rag_backend/user_files.py:sanitize_filename
#--- Bereinigt Dateinamen fuer data/ und out/ nach denselben Regeln.
def sanitize_filename(raw: str) -> str:
    """Bereinigt einen Dateinamen (Basename) fuer data/ und out/."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = name.lstrip(".")
    name = name.rstrip(". ")
    if not name or name in {".", ".."}:
        raise ValueError("Unbrauchbarer Dateiname: Name ist leer.")

    name = _SAFE_SANDBOX_NAME_RE.sub("_", name)
    name = name.rstrip(". ")
    if not name:
        raise ValueError("Unbrauchbarer Dateiname: Keine zulaessigen Zeichen vorhanden.")

    if len(name) > 180:
        name = name[:180]
    while len(name.encode("utf-8")) > 240:
        name = name[:-1]
    name = name.rstrip(". ")
    if not name:
        raise ValueError("Unbrauchbarer Dateiname nach Laengenbegrenzung.")

    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES:
        raise ValueError(f"Unbrauchbarer Dateiname: Windows-Geraetename {stem!r} ist nicht zulaessig.")

    return name


def _harden_service_process() -> None:
    """Setzt den Dienstprozess auf nicht-dumpbar (PR_SET_DUMPABLE = 0), damit
    andere Benutzer ihn weder per ptrace untersuchen noch sensible Dateien unter
    /proc/<pid>/ (environ, maps, mem, fd, auxv) einsehen koennen."""
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_DUMPABLE = 4, arg2 = 0 (nicht dumpbar)
        libc.prctl(4, 0, 0, 0, 0)
    except Exception:
        pass


_harden_service_process()


#--- Gemeinsames Geheimnis mit dem Backend (SANDBOX_TOKEN aus der .env). Die Repliken
#--- haengen im selben Netz: Ohne Token koennte Code in einer Replik eine andere
#--- ueber /execute beanspruchen und sie so fuer den naechsten echten Lauf verbrauchen,
#--- oder den eigenen Dienst auf 127.0.0.1:8001 ansprechen. Der Wert wird aus der
#--- Umgebung genommen, sobald er gelesen ist; Laeufe bekommen ohnehin eine eigene,
#--- leere Umgebung, und /proc/1/environ ist fuer runner und calc nicht lesbar
#--- (Dienst nicht dumpbar). Ohne Wert (alte .env) prueft die Sandbox nichts.
_SANDBOX_TOKEN = os.environ.pop("SANDBOX_TOKEN", "").strip()
if not _SANDBOX_TOKEN:
    print("[calc-sandbox] WARNUNG: SANDBOX_TOKEN fehlt -- Anfragen werden ohne Token "
          "angenommen. setup/init_stack.py legt ihn in der .env an.", flush=True)


def _token_ok(given: str | None) -> bool:
    if not _SANDBOX_TOKEN:
        return True
    return hmac.compare_digest((given or "").encode("utf-8"), _SANDBOX_TOKEN.encode("utf-8"))


@app.middleware("http")
async def _require_sandbox_token(request, call_next):
    if request.url.path != "/healthz" and not _token_ok(request.headers.get("x-sandbox-token")):
        return JSONResponse(status_code=401, content={"error": "Nicht autorisiert."})
    return await call_next(request)


def _has_required_capabilities() -> bool:
    """Prueft ueber /proc/self/status, ob SETUID, SETGID und KILL in CapEff gesetzt sind."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if line.startswith("CapEff:"):
                    cap_eff = int(line.split(":", 1)[1].strip(), 16)
                    return (cap_eff & _REQUIRED_CAPS_MASK) == _REQUIRED_CAPS_MASK
    except OSError:
        return False
    return False


def _can_switch_to_runner() -> bool:
    """Prueft, ob der Dienst im Container als root mit SETUID/SETGID/KILL laeuft
    und in die unprivilegierten Lauf-Benutzer (runner 10001, calc 10002) wechseln kann."""
    return (
        os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and _has_required_capabilities()
    )


def _ensure_container_privilege_drop() -> None:
    """Verhindert im Container (ARGUS_SANDBOX_CONTAINER=1) einen stillen Rueckfall
    auf die Dienst-UID, falls der Dienst nicht als root mit SETUID/SETGID/KILL laeuft."""
    if os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1" and not _can_switch_to_runner():
        raise RuntimeError(_MISCONFIG_MSG)


def _drop_to_runner_in_child(uid: int = _RUNNER_UID, gid: int = _RUNNER_GID) -> None:
    """Wechselt im geforkten Kindprozess vollstaendig zum angegebenen Lauf-Benutzer
    (runner mit uid/gid 10001 fuer Code bzw. calc mit uid/gid 10002 fuer Rechnungen,
    jeweils mit leeren Zusatzgruppen). Durch den Wechsel von uid 0 auf eine
    unprivilegierte uid loescht der Linux-Kernel automatisch alle Capabilities."""
    if os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1":
        if not _can_switch_to_runner():
            raise RuntimeError(_MISCONFIG_MSG)
        os.setgroups([])
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)


def _current_address_space_bytes() -> int:
    """Liest die aktuelle virtuelle Adressraumgroesse (in Bytes) aus /proc/self/statm."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fp:
            pages = int(fp.read().split()[0])
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size
    except Exception:
        pass
    return 0


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


_DUNDER_RE = _re.compile(r"__\w+__")
_QUOTE_CHARS = ("'", '"', "`")
# Attributzugriff: Punkt gefolgt von (optionalem Leerraum und) Buchstabe/Unterstrich.
# Dezimalzahlen wie 1.5, .5 oder 3.2e-3 haben nach dem Punkt eine Ziffer und bleiben frei.
_ATTR_RE = _re.compile(r"\.\s*[A-Za-z_]")
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "input", "exit", "quit",
    "globals", "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "hasattr", "__import__", "breakpoint", "help",
}

_EXEC_OUTPUT_CAP = 20000
_EXEC_CODE_CAP = 20000
_OUT_MAX_FILES = 10
_OUT_MAX_BYTES = 20 * 1024 * 1024
_IN_MAX_FILES = 20
_IN_MAX_BYTES = 64 * 1024 * 1024

# Getrennte Sperren fuer Code-Ausfuehrung (runner, uid 10001) und Rechnungen
# (calc, uid 10002), damit /calculate nicht hinter einem laufenden /execute wartet.
_EXEC_LOCK = threading.Lock()
_CALC_LOCK = threading.Lock()
_EXTRA_PROTECTED_PIDS: set[int] = set()

#--- Kindprozesse, auf die ein Pfad selbst wartet: der Lauf von /execute und der
#--- Rechenprozess von /calculate. Seit beide Pfade nebeneinander laufen, darf das
#--- Ernten verwaister Prozesse (_reap_zombies) sie dem anderen Pfad nicht
#--- wegnehmen: der Rueckgabewert ginge verloren, und ein fehlgeschlagener Lauf
#--- saehe aus wie ein gelungener. Die Sperre haelt Starten+Eintragen und das
#--- Ernten auseinander.
_CHILD_LOCK = threading.Lock()
_TRACKED_CHILD_PIDS: set[int] = set()


def _untrack_child(pid: int | None) -> None:
    if pid is not None:
        with _CHILD_LOCK:
            _TRACKED_CHILD_PIDS.discard(pid)


def _eval_sympy_expr(expr: str, label: str) -> str:
    """Wertet den bereits geprueften Sympy-Ausdruck aus."""
    try:
        parsed = parse_expr(
            expr,
            transformations=_SYMPY_TRANSFORMATIONS,
            local_dict=_SAFE_LOCAL_DICT,
            global_dict={"__builtins__": {}},
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
    except MemoryError:
        return "Berechnungsfehler: Speicherlimit ueberschritten."
    except Exception as e:
        return f"Berechnungsfehler: {type(e).__name__}: {str(e)}"


def _run_sympy_in_process(expr: str, label: str, timeout: float = _CALC_TIMEOUT_SEC) -> str:
    """Fuehrt die Sympy-Auswertung in einem eigenen Kindprozess unter dem Rechen-Benutzer
    calc (uid/gid 10002) im Wurzelverzeichnis (/) mit harten Grenzen fuer Laufzeit,
    CPU-Zeit und Speicher (Istwert aus /proc/self/statm plus 256 MB) aus."""
    try:
        _ensure_container_privilege_drop()
    except RuntimeError as e:
        return f"Berechnungsfehler: {e}"

    if not hasattr(os, "fork"):
        return _eval_sympy_expr(expr, label)

    in_container = os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1"
    skip_proc_cleanup = os.getenv("CALC_SANDBOX_SKIP_PROC_CLEANUP", "0") == "1"

    if in_container and not skip_proc_cleanup:
        _kill_other_processes(target_uid=_CALC_UID, target_gid=_CALC_GID)

    r_fd, w_fd = os.pipe()
    with _CHILD_LOCK:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid > 0:
            _TRACKED_CHILD_PIDS.add(pid)
    if pid == 0:
        try:
            os.close(r_fd)
            os.setsid()
            os.chdir("/")
            _drop_to_runner_in_child(uid=_CALC_UID, gid=_CALC_GID)
            resource.setrlimit(resource.RLIMIT_CPU, _CALC_CPU_LIMIT_SEC)
            mem_limit = _current_address_space_bytes() + _CALC_MEM_LIMIT_BYTES
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
            except (ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_DATA, (_CALC_MEM_LIMIT_BYTES, _CALC_MEM_LIMIT_BYTES))
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            res_str = _eval_sympy_expr(expr, label)
            data = res_str.encode("utf-8", "replace")
            view = memoryview(data)
            while view:
                written = os.write(w_fd, view)
                view = view[written:]
            os.close(w_fd)
        except MemoryError:
            try:
                os.write(w_fd, b"Berechnungsfehler: Speicherlimit ueberschritten.")
                os.close(w_fd)
            except Exception:
                pass
        except BaseException as exc:
            try:
                msg = f"Berechnungsfehler: {type(exc).__name__}: {exc}".encode("utf-8", "replace")[:2000]
                os.write(w_fd, msg)
                os.close(w_fd)
            except Exception:
                pass
        finally:
            os._exit(0)

    os.close(w_fd)
    chunks: list[bytes] = []
    total_read = 0
    timed_out = False
    status = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([r_fd], [], [], rem)
            if not ready:
                timed_out = True
                break
            chunk = os.read(r_fd, 65536)
            if not chunk:
                break
            if total_read < 65536:
                chunks.append(chunk)
                total_read += len(chunk)
    finally:
        try:
            os.close(r_fd)
        except OSError:
            pass
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            wpid, status = pid, 0
        if wpid == 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                status = 0
        _untrack_child(pid)
        if in_container and not skip_proc_cleanup:
            _kill_other_processes(target_uid=_CALC_UID, target_gid=_CALC_GID)

    if timed_out:
        return f"Berechnungsfehler: Timeout nach {timeout:.0f} Sekunden (Ausdruck zu komplex)."

    out_str = b"".join(chunks).decode("utf-8", "replace")
    if not out_str:
        if os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            if sig in (signal.SIGXCPU, signal.SIGKILL):
                return f"Berechnungsfehler: Timeout nach {timeout:.0f} Sekunden (Ausdruck zu komplex)."
            return "Berechnungsfehler: Speicher- oder Ressourcenlimit ueberschritten."
        return "Berechnungsfehler: Unbekannter Rechenfehler."
    return out_str


def _run(expr: str, timeout: float = _CALC_TIMEOUT_SEC) -> str:
    expr = expr.strip()
    if not expr:
        return "Berechnungsfehler: Leerer Ausdruck."
    if len(expr) > 2000:
        return "Berechnungsfehler: Ausdruck zu lang."
    if not expr.isascii():
        return "Berechnungsfehler: Nicht-ASCII-Zeichen nicht erlaubt."
    #--- Rueckstrich, Kommentar und Zeilenumbruch koennen zwischen Punkt und Namen
    #--- stehen (x.\<Zeilenumbruch>name, x.#c<Zeilenumbruch>name) und so die
    #--- Pruefung auf Attributzugriff umgehen. Eine Rechnung braucht keins davon.
    if any(c in expr for c in ("\\", "#")) or any(ord(c) < 32 and c != "\t" for c in expr):
        return "Berechnungsfehler: Rueckstrich, Kommentar und Zeilenumbruch nicht erlaubt."
    if any(q in expr for q in _QUOTE_CHARS):
        return "Berechnungsfehler: Zeichenketten nicht erlaubt."
    if _ATTR_RE.search(expr):
        return "Berechnungsfehler: Attributzugriff nicht erlaubt."
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

    return _run_sympy_in_process(expr, label, timeout=timeout)


def _calculate_serialized(expr: str) -> str:
    """Fuehrt /calculate unter der eigenen Rechen-Sperre (_CALC_LOCK) aus, damit
    Rechnungen unter calc (uid 10002) nicht auf /execute (runner, uid 10001) warten."""
    try:
        _ensure_container_privilege_drop()
    except RuntimeError as e:
        return f"Berechnungsfehler: {e}"

    acquired = _CALC_LOCK.acquire(timeout=10.0)
    if not acquired:
        return "Berechnungsfehler: Rechen-Sandbox belegt, bitte erneut versuchen."
    try:
        return _run(expr, timeout=_CALC_TIMEOUT_SEC)
    except RuntimeError as e:
        return f"Berechnungsfehler: {str(e)}"
    finally:
        _CALC_LOCK.release()


@app.post("/calculate")
async def calculate(req: CalcRequest):
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_calculate_serialized, req.expression),
            timeout=10.0 + _CALC_TIMEOUT_SEC + 5.0,
        )
        if len(result) > 10000:
            result = result[:10000] + "... [gekuerzt]"
        return {"result": result}
    except asyncio.TimeoutError:
        return {"result": "Berechnungsfehler: Timeout nach 5 Sekunden (Ausdruck zu komplex)."}
    except Exception as e:
        return {"result": f"Berechnungsfehler: {str(e)}"}


def _reap_zombies() -> None:
    """Erntet beendete Prozesse, deren Elternprozess dieser Dienst ist oder geworden
    ist (er laeuft im Container als PID 1 und erbt Verwaiste). Ohne das blieben sie
    als Zombies stehen und zaehlten gegen pids_limit.
    Ausgenommen sind die Kindprozesse in _TRACKED_CHILD_PIDS: auf sie wartet ein Pfad
    selbst. Deshalb einzeln ueber /proc statt waitpid(-1), das JEDES Kind naehme."""
    if not hasattr(os, "waitpid") or not os.path.isdir("/proc"):
        return
    me = os.getpid()
    with _CHILD_LOCK:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in _TRACKED_CHILD_PIDS:
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as fp:
                    raw = fp.read()
            except OSError:
                continue
            #--- Aufbau: "pid (name) zustand elternpid ...". Der Name kann Klammern
            #--- und Leerzeichen enthalten, deshalb ab der LETZTEN Klammer lesen.
            fields = raw.rsplit(b")", 1)[-1].split()
            if len(fields) < 2 or fields[0] != b"Z":
                continue
            try:
                if int(fields[1]) != me:
                    continue
            except ValueError:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass


def _kill_all_as_user(uid: int = _RUNNER_UID, gid: int = _RUNNER_GID) -> None:
    """Startet einen kurzen Helferprozess, der zur angegebenen uid/gid wechselt
    und per kill(-1, SIGKILL) atomar allen Prozessen dieses Benutzers SIGKILL schickt."""
    if not _can_switch_to_runner():
        return
    try:
        subprocess.run(
            ["/bin/sh", "-c", "kill -9 -1"],
            user=uid,
            group=gid,
            extra_groups=[],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
    except Exception:
        pass


def _list_runner_processes(keep_pids: set[int], target_uid: int = _RUNNER_UID) -> list[int]:
    """Liefert alle aktiven (nicht-Zombie) PIDs in /proc, die target_uid gehoeren."""
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in keep_pids:
            continue
        status_path = f"/proc/{pid}/status"
        try:
            with open(status_path, "r", encoding="utf-8", errors="replace") as fp:
                status_text = fp.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        state_char = ""
        uids: set[int] = set()
        for line in status_text.splitlines():
            if line.startswith("State:"):
                parts = line.split(":", 1)[1].strip()
                if parts:
                    state_char = parts[0]
            elif line.startswith("Uid:"):
                for tok in line.split(":", 1)[1].split():
                    if tok.isdigit():
                        uids.add(int(tok))
        if state_char in ("Z", "X"):
            continue
        if target_uid in uids or (hasattr(os, "geteuid") and os.geteuid() != 0):
            found.append(pid)
    return found


def _kill_other_processes(
    protected_pids: set[int] | None = None,
    target_uid: int = _RUNNER_UID,
    target_gid: int = _RUNNER_GID,
) -> None:
    """Beendet alle Prozesse des angegebenen Benutzers (target_uid) ueber einen Helfer
    (kill -9 -1 unter target_uid) sowie per SIGKILL und prueft ueber /proc in mehreren
    Durchgaengen, bis nachweislich kein Prozess dieser UID mehr existiert. Laesst sich
    das nicht bestaetigen, wird ein RuntimeError ausgeloest (Fail-Closed)."""
    if not os.path.isdir("/proc"):
        if os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1":
            raise RuntimeError("Sandbox-Aufraeumen fehlgeschlagen: /proc nicht verfuegbar.")
        return

    keep_pids = {1, os.getpid()}
    try:
        ppid = os.getppid()
        if ppid > 0:
            keep_pids.add(ppid)
    except Exception:
        pass
    if protected_pids:
        keep_pids.update(protected_pids)
    keep_pids.update(_EXTRA_PROTECTED_PIDS)

    remaining: list[int] = []
    for attempt in range(_KILL_MAX_ATTEMPTS):
        # 1. Helfer wechselt zu target_uid und schickt allen eigenen Prozessen SIGKILL
        _kill_all_as_user(uid=target_uid, gid=target_gid)
        _reap_zombies()

        # 2. Kontrolle ueber /proc; falls noch Prozesse dieser UID da sind, direkt SIGKILL senden
        remaining = _list_runner_processes(keep_pids, target_uid=target_uid)
        if not remaining:
            return

        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        _reap_zombies()
        remaining = _list_runner_processes(keep_pids, target_uid=target_uid)
        if not remaining:
            return
        if attempt + 1 < _KILL_MAX_ATTEMPTS:
            time.sleep(0.02)

    raise RuntimeError(
        f"Sandbox-Aufraeumen fehlgeschlagen: Prozesse des Benutzers {target_uid} konnten nicht beendet werden (PIDs: {remaining})."
    )


def _purge_dir_contents(root_dir: str) -> None:
    """Loescht rekursiv alle Eintraege innerhalb von root_dir (nicht root_dir selbst)
    und korrigiert zuvor enge Verzeichnisrechte (z. B. chmod 000)."""
    if not os.path.isdir(root_dir):
        return

    def _purge_entry(path: str) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
            try:
                for sub in os.listdir(path):
                    _purge_entry(os.path.join(path, sub))
            except OSError:
                pass
            try:
                os.rmdir(path)
            except OSError:
                shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass

    try:
        for name in os.listdir(root_dir):
            _purge_entry(os.path.join(root_dir, name))
    except OSError:
        pass


def _clean_tmp_dir() -> None:
    """Loescht alle Dateien und Ordner innerhalb von /tmp (nicht /tmp selbst).
    Da der Dienst ohne CAP_DAC_OVERRIDE laeuft, entfernt zuerst ein Helfer unter
    dem Code-Lauf-Benutzer runner (uid 10001) dessen Dateien/Ordner (inklusive
    chmod-000-Ordnern) und danach der Dienst selbst alle uebrigen Eintraege."""
    if not os.path.isdir("/tmp"):
        return
    dirs_to_clean = ["/tmp"]
    if os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1":
        for extra_dir in ("/dev/shm", "/dev/mqueue"):
            if os.path.isdir(extra_dir):
                dirs_to_clean.append(extra_dir)

    if hasattr(os, "fork") and _can_switch_to_runner():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            try:
                _drop_to_runner_in_child(uid=_RUNNER_UID, gid=_RUNNER_GID)
                for d in dirs_to_clean:
                    _purge_dir_contents(d)
            except BaseException:
                pass
            finally:
                os._exit(0)
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    for d in dirs_to_clean:
        _purge_dir_contents(d)


def _unlock_out_files_as_runner(out_dir: str) -> None:
    """Stellt als Lauf-Benutzer runner (uid 10001) sicher, dass normale Dateien in out/
    Leserechte (0644) haben, damit der Dienst sie auch ohne CAP_DAC_OVERRIDE lesen kann,
    selbst wenn der ausgefuehrte Code sie mit engen Rechten (z. B. 0600) angelegt hat."""
    if not os.path.isdir(out_dir) or not hasattr(os, "fork") or not _can_switch_to_runner():
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        try:
            _drop_to_runner_in_child(uid=_RUNNER_UID, gid=_RUNNER_GID)
            for entry in os.scandir(out_dir):
                try:
                    st = os.lstat(entry.path)
                    if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                        os.chmod(entry.path, 0o644)
                except OSError:
                    pass
        except BaseException:
            pass
        finally:
            os._exit(0)
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _cleanup_sandbox(protected_pids: set[int] | None = None, kill_processes: bool = True) -> None:
    """Raeumt die Code-Sandbox vollstaendig auf (vor und nach jedem /execute-Lauf).
    Alle Prozesse des Code-Lauf-Benutzers runner (uid 10001) werden beendet und
    ueber /proc geprueft, danach wird /tmp geleert."""
    #--- Nur im Sandbox-Container (ENV in Dockerfile.sandbox). Ausserhalb, etwa bei
    #--- Tests auf einem Entwicklerrechner oder CI-Runner, wuerde das fremde Prozesse
    #--- beenden und /tmp des Rechners leeren.
    if os.environ.get("ARGUS_SANDBOX_CONTAINER") != "1":
        return
    if kill_processes and os.getenv("CALC_SANDBOX_SKIP_PROC_CLEANUP", "0") != "1":
        _kill_other_processes(
            protected_pids=protected_pids,
            target_uid=_RUNNER_UID,
            target_gid=_RUNNER_GID,
        )
    _clean_tmp_dir()


# Runner laeuft als isolierter Kindprozess (python -I -B) unter dem unprivilegierten
# Benutzer "runner" (uid 10001) mit eigenen rlimits als Defense-in-Depth.
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
    files: list[dict] = []


def _exec_code(
    code: str,
    language: str,
    timeout: float,
    files: list[dict] | None = None,
    protected_pids: set[int] | None = None,
    kill_processes: bool = True,
) -> dict:
    code = (code or "").strip()
    if not code:
        return {"error": "Leerer Code."}
    if len(code) > _EXEC_CODE_CAP:
        return {"error": f"Code zu lang (max {_EXEC_CODE_CAP} Zeichen)."}
    if (language or "python").lower() != "python":
        return {"error": f"Nicht unterstuetzte Sprache '{language}'."}
    try:
        t = max(1.0, min(float(timeout or 15.0), 30.0))
    except Exception:
        t = 15.0

    in_files = files or []
    if len(in_files) > _IN_MAX_FILES:
        return {"error": f"Zu viele Eingabedateien (max {_IN_MAX_FILES})."}

    decoded_files: list[tuple[str, bytes]] = []
    total_in_bytes = 0
    for f in in_files:
        if not isinstance(f, dict):
            continue
        raw_name = f.get("name") or ""
        try:
            clean_name = sanitize_filename(raw_name)
        except ValueError:
            continue
        if not clean_name:
            continue
        raw_b64 = f.get("content") or f.get("content_base64") or ""
        try:
            data_bytes = base64.b64decode(raw_b64)
        except Exception:
            continue
        total_in_bytes += len(data_bytes)
        if total_in_bytes > _IN_MAX_BYTES:
            return {"error": f"Eingabedateien zu gross (max {_IN_MAX_BYTES // (1024 * 1024)} MB)."}
        decoded_files.append((clean_name, data_bytes))

    try:
        _ensure_container_privilege_drop()
    except RuntimeError as e:
        return {"error": str(e)}

    # Serialisiere Code-Laeufe: warte hoechstens 30s auf das Lock
    acquired = _EXEC_LOCK.acquire(timeout=30.0)
    if not acquired:
        return {"error": "Sandbox belegt, bitte erneut versuchen."}

    result_payload: dict
    try:
        # Vor dem Lauf: Reste frueherer Laeufe und verwaiste Prozesse saeubern.
        # Laesst sich nicht bestaetigen, dass kein Prozess des Lauf-Benutzers mehr
        # existiert, bricht die Sandbox ab (Fail-Closed).
        try:
            _cleanup_sandbox(protected_pids=protected_pids, kill_processes=kill_processes)
        except RuntimeError as e:
            return {"error": str(e)}

        tmp_root = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        run_dir = tempfile.mkdtemp(prefix="exec_run_", dir=tmp_root)
        tracked_pid: int | None = None
        try:
            data_dir = os.path.join(run_dir, "data")
            out_dir = os.path.join(run_dir, "out")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            # run_dir und out_dir mit Sticky-Bit fuer runner beschreibbar machen;
            # data_dir bleibt schreibgeschuetzt (0755/0644, gehoert root).
            try:
                os.chmod(run_dir, 0o1777)
                os.chmod(data_dir, 0o755)
                os.chmod(out_dir, 0o1777)
            except OSError:
                pass

            for clean_name, data_bytes in decoded_files:
                target_file = os.path.join(data_dir, clean_name)
                try:
                    with open(target_file, "wb") as out_fp:
                        out_fp.write(data_bytes)
                    os.chmod(target_file, 0o644)
                except Exception:
                    continue

            popen_kwargs: dict = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "start_new_session": True,
                "cwd": run_dir,
                "env": {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                },
            }
            if _can_switch_to_runner():
                popen_kwargs["user"] = _RUNNER_UID
                popen_kwargs["group"] = _RUNNER_GID
                popen_kwargs["extra_groups"] = []

            #--- Starten und Eintragen unter einer Sperre: sonst koennte der Rechenpfad
            #--- einen sofort beendeten Lauf ernten, bevor er eingetragen ist.
            with _CHILD_LOCK:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-B", "-c", _EXEC_RUNNER],
                    **popen_kwargs,
                )
                tracked_pid = proc.pid
                _TRACKED_CHILD_PIDS.add(tracked_pid)
            try:
                out, err = proc.communicate(input=code.encode("utf-8"), timeout=t)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                _untrack_child(tracked_pid)
                result_payload = {"error": f"Timeout nach {t:.0f}s (Sandbox-Prozess hart beendet)."}
            else:
                _untrack_child(tracked_pid)
                # Zuerst alle Prozesse des Lauf-Benutzers (runner, uid 10001) beenden und
                # ueber /proc pruefen, bevor Rechte in out/ geoeffnet und Dateien eingesammelt
                # werden. So kann kein Hintergrundprozess out/ waehrend des Lesens veraendern.
                if (
                    os.environ.get("ARGUS_SANDBOX_CONTAINER") == "1"
                    and kill_processes
                    and os.getenv("CALC_SANDBOX_SKIP_PROC_CLEANUP", "0") != "1"
                ):
                    _kill_other_processes(
                        protected_pids=protected_pids,
                        target_uid=_RUNNER_UID,
                        target_gid=_RUNNER_GID,
                    )

                stdout = out.decode("utf-8", "replace")
                stderr = err.decode("utf-8", "replace")
                if len(stdout) > _EXEC_OUTPUT_CAP:
                    stdout = stdout[:_EXEC_OUTPUT_CAP] + chr(10) + "... [gekuerzt]"

                # Nach dem Beenden aller Prozesse etwaige enge Dateirechte (z. B. 0600) in out/ oeffnen
                _unlock_out_files_as_runner(out_dir)

                # Sammle Dateien direkt aus out/
                out_files = []
                out_bytes_total = 0
                omitted_count = 0
                if os.path.isdir(out_dir):
                    try:
                        entries = sorted(os.scandir(out_dir), key=lambda e: e.name.lower())
                    except Exception:
                        entries = []
                    for entry in entries:
                        try:
                            st = os.lstat(entry.path)
                            # Nur normale Dateien direkt in out/ (keine Unterordner, keine Symlinks)
                            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                                continue
                            try:
                                clean_out_name = sanitize_filename(entry.name)
                            except ValueError:
                                continue
                            if not clean_out_name:
                                continue
                            file_size = st.st_size
                            if len(out_files) >= _OUT_MAX_FILES or (out_bytes_total + file_size) > _OUT_MAX_BYTES:
                                omitted_count += 1
                                continue
                            open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                            fd = os.open(entry.path, open_flags)
                            try:
                                with os.fdopen(fd, "rb") as fp:
                                    file_data = fp.read()
                            except Exception:
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                                raise
                            out_files.append({
                                "name": clean_out_name,
                                "content": base64.b64encode(file_data).decode("ascii"),
                                "size": len(file_data),
                            })
                            out_bytes_total += len(file_data)
                        except Exception:
                            continue

                if omitted_count > 0:
                    hinweis = f"\n[Hinweis: {omitted_count} Datei(en) in out/ weggelassen (Limit: max {_OUT_MAX_FILES} Dateien / {_OUT_MAX_BYTES // (1024 * 1024)} MB)]"
                    if proc.returncode == 0:
                        stdout = (stdout + hinweis)[:_EXEC_OUTPUT_CAP]
                    else:
                        stderr = (stderr + hinweis)[:_EXEC_OUTPUT_CAP]

                if proc.returncode != 0:
                    msg = stderr.strip() or "Unbekannter Ausfuehrungsfehler."
                    if stdout.strip():
                        msg = msg + chr(10) + "--- stdout ---" + chr(10) + stdout
                    result_payload = {"error": msg[:_EXEC_OUTPUT_CAP], "files": out_files}
                else:
                    if stderr.strip():
                        stdout = (stdout + chr(10) + "--- stderr ---" + chr(10) + stderr)[:_EXEC_OUTPUT_CAP]
                    result_payload = {"result": stdout, "files": out_files}
        finally:
            #--- Auch nach einem unerwarteten Fehler austragen, damit das Aufraeumen
            #--- einen liegengebliebenen Lauf ernten kann.
            _untrack_child(tracked_pid)
            try:
                _cleanup_sandbox(protected_pids=protected_pids, kill_processes=kill_processes)
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)
        return result_payload
    except RuntimeError as e:
        return {"error": str(e)}
    finally:
        _EXEC_LOCK.release()


#--- Ein Code-Lauf je Container (SANDBOX_ONE_RUN, Standard aus).
#--- An: Das erste /execute beansprucht den Container, jedes weitere bekommt sofort
#--- 503 "Sandbox wird erneuert.". Nach dem Lauf beendet sich der Dienst (erst wenn
#--- die Antwort verschickt ist), Docker startet den Container ueber die
#--- Restart-Policy frisch: leeres tmpfs, neue Namensraeume, keine Reste des Laufs.
#--- Docker verdoppelt die Wartezeit vor dem Neustart, wenn ein Container keine
#--- 10 s lief; darum endet der Dienst fruehestens 10 s nach seinem Start und weist
#--- bis dahin weiter ab. /calculate beansprucht nicht und bleibt bis zum Ende erreichbar.
_SANDBOX_RENEW_MSG = "Sandbox wird erneuert."
_ONE_RUN_MIN_LIFETIME_SEC = 10.0
#--- Sicherheitsnetz: Laeuft die Hintergrundaufgabe nach der Antwort nie (z. B. weil
#--- der Client mitten in der Antwort abbricht), beendet dieser Zeitgeber den
#--- beanspruchten Container trotzdem. Groesser als Watchdog (30 + 30 + 15 s) plus
#--- Mindestlaufzeit, damit er einen regulaeren Lauf nie abschneidet.
_ONE_RUN_FALLBACK_SEC = 150.0
#--- Faehrt uvicorn nach SIGTERM nicht binnen dieser Zeit herunter, harter Ausstieg.
_RETIRE_HARD_EXIT_SEC = 30.0
_SERVICE_STARTED_AT = time.monotonic()
_CLAIM_LOCK = threading.Lock()
_CLAIMED = False
_RETIRED = False
_FALLBACK_TIMER: threading.Timer | None = None


def _one_run_enabled() -> bool:
    return os.environ.get("SANDBOX_ONE_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def _claim_container() -> bool:
    """Beansprucht den Container fuer genau einen Code-Lauf. True nur beim ersten
    Aufruf, auch bei gleichzeitigen Anfragen (Sperre _CLAIM_LOCK)."""
    global _CLAIMED, _FALLBACK_TIMER
    with _CLAIM_LOCK:
        if _CLAIMED:
            return False
        _CLAIMED = True
        timer = threading.Timer(_ONE_RUN_FALLBACK_SEC, _retire_once)
        timer.daemon = True
        _FALLBACK_TIMER = timer
    timer.start()
    return True


def _retire() -> None:
    """Beendet den Dienst. SIGTERM an sich selbst: uvicorn nimmt keine neuen
    Verbindungen mehr an, laesst laufende Anfragen (/calculate) fertig werden und
    endet mit Code 0. Haengt das Herunterfahren, folgt ein harter Ausstieg.
    Tests ersetzen diese Funktion."""
    hard_exit = threading.Timer(_RETIRE_HARD_EXIT_SEC, os._exit, args=(0,))
    hard_exit.daemon = True
    hard_exit.start()
    os.kill(os.getpid(), signal.SIGTERM)


def _retire_once() -> None:
    """Ruft _retire hoechstens einmal auf (Hintergrundaufgabe und Sicherheitsnetz)."""
    global _RETIRED
    with _CLAIM_LOCK:
        if _RETIRED:
            return
        _RETIRED = True
        timer = _FALLBACK_TIMER
    if timer is not None:
        timer.cancel()
    _retire()


#--- Wartefunktion vor dem Beenden; Tests ersetzen sie, statt 10 s zu warten.
_retire_wait = asyncio.sleep


async def _retire_after_run() -> None:
    """Laeuft als BackgroundTask, also erst nachdem die Antwort verschickt ist."""
    remaining = _ONE_RUN_MIN_LIFETIME_SEC - (time.monotonic() - _SERVICE_STARTED_AT)
    if remaining > 0:
        await _retire_wait(remaining)
    _retire_once()


@app.post("/execute")
async def execute(req: ExecRequest, background_tasks: BackgroundTasks):
    if _one_run_enabled():
        if not _claim_container():
            return JSONResponse(status_code=503, content={"error": _SANDBOX_RENEW_MSG})
        #--- Gilt fuer jeden Ausgang des Laufs (Erfolg, Fehler, Watchdog), denn alle
        #--- Zweige unten liefern eine Antwort.
        background_tasks.add_task(_retire_after_run)
    # Echter Sicherheitsrahmen = Container: Dienst als root (nicht dumpbar) mit
    # cap_drop ALL + SETUID/SETGID/KILL, Ausfuehrung als unprivilegierter Benutzer
    # runner (uid 10001) ohne Capabilities, no-new-privileges, read_only rootfs, Netz ohne Egress.
    try:
        # Watchdog: 30s Wartezeit auf Lock + max 30s Ausfuehrung + 15s Puffer
        max_run = min(float(req.timeout or 15.0), 30.0)
        watchdog_timeout = 30.0 + max_run + 15.0
        return await asyncio.wait_for(
            asyncio.to_thread(_exec_code, req.code, req.language, req.timeout, req.files),
            timeout=watchdog_timeout,
        )
    except asyncio.TimeoutError:
        return {"error": "Sandbox-Watchdog-Timeout."}
    except Exception as e:
        return {"error": f"Sandbox-Fehler: {type(e).__name__}: {e}"}


@app.get("/healthz")
async def health():
    return {"status": "ok"}
