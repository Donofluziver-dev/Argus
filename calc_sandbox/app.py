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

def _run(expr: str) -> str:
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

import base64
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

_EXEC_OUTPUT_CAP = 20000
_EXEC_CODE_CAP = 20000
_OUT_MAX_FILES = 10
_OUT_MAX_BYTES = 20 * 1024 * 1024
_IN_MAX_FILES = 20
_IN_MAX_BYTES = 64 * 1024 * 1024

# Da alle Ausfuehrungen im Container als derselbe Benutzer "sandbox" laufen und
# sich das /tmp-Verzeichnis teilen, muessen Laeufe strikt sequentiell ausgefuehrt
# werden. Ein globaler Lock verhindert, dass parallele Laeufe die /tmp-Dateien
# oder Daten des jeweils anderen auslesen koennen.
_EXEC_LOCK = threading.Lock()
_EXTRA_PROTECTED_PIDS: set[int] = set()


def _clean_tmp_dir() -> None:
    """Loescht alle Dateien und Ordner innerhalb von /tmp (nicht /tmp selbst)."""
    if not os.path.isdir("/tmp"):
        return
    try:
        for name in os.listdir("/tmp"):
            full_p = os.path.join("/tmp", name)
            try:
                if os.path.islink(full_p) or os.path.isfile(full_p):
                    os.unlink(full_p)
                elif os.path.isdir(full_p):
                    shutil.rmtree(full_p, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def _kill_other_processes(protected_pids: set[int] | None = None) -> None:
    """Beendet alle Prozesse ausser PID 1, dem Serverprozess und geschuetzten PIDs per SIGKILL.
    Uvicorn laeuft im Container als PID 1."""
    if not os.path.isdir("/proc"):
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

    try:
        killed_any = False
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    pid = int(entry)
                    if pid not in keep_pids:
                        os.kill(pid, signal.SIGKILL)
                        killed_any = True
                except (OSError, ValueError):
                    pass
        if killed_any:
            time.sleep(0.05)
    except Exception:
        pass


def _cleanup_sandbox(protected_pids: set[int] | None = None, kill_processes: bool = True) -> None:
    """Raeumt die Sandbox vollstaendig auf (vor und nach jedem Lauf).
    Da alle Laeufe dieselbe UID nutzen und sich /tmp teilen, muessen verwaiste
    Hintergrundprozesse (z. B. via os.fork + os.setsid) beendet und alle Dateien
    in /tmp entfernt werden."""
    #--- Nur im Sandbox-Container (ENV in Dockerfile.sandbox). Ausserhalb, etwa bei
    #--- Tests auf einem Entwicklerrechner oder CI-Runner, wuerde das fremde Prozesse
    #--- beenden und /tmp des Rechners leeren.
    if os.environ.get("ARGUS_SANDBOX_CONTAINER") != "1":
        return
    if kill_processes and os.getenv("CALC_SANDBOX_SKIP_PROC_CLEANUP", "0") != "1":
        _kill_other_processes(protected_pids=protected_pids)
    _clean_tmp_dir()


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
        clean_name = os.path.basename(raw_name.replace("\\", "/")).strip()
        clean_name = _re.sub(r"[^A-Za-z0-9._ ()\-]", "_", clean_name).lstrip(".")
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

    # Serialisiere Sandbox-Laeufe: warte hoechstens 30s auf das Lock
    acquired = _EXEC_LOCK.acquire(timeout=30.0)
    if not acquired:
        return {"error": "Sandbox belegt, bitte erneut versuchen."}

    try:
        # Vor dem Lauf: Reste frueherer Laeufe und verwaiste Prozesse saeubern
        _cleanup_sandbox(protected_pids=protected_pids, kill_processes=kill_processes)

        tmp_root = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        run_dir = tempfile.mkdtemp(prefix="exec_run_", dir=tmp_root)
        try:
            data_dir = os.path.join(run_dir, "data")
            out_dir = os.path.join(run_dir, "out")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            for clean_name, data_bytes in decoded_files:
                target_file = os.path.join(data_dir, clean_name)
                try:
                    with open(target_file, "wb") as out_fp:
                        out_fp.write(data_bytes)
                except Exception:
                    continue

            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", _EXEC_RUNNER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, cwd=run_dir,
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
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        clean_out_name = _re.sub(r"[^A-Za-z0-9._ ()\-]", "_", entry.name).lstrip(".")
                        if not clean_out_name:
                            continue
                        file_size = st.st_size
                        if len(out_files) >= _OUT_MAX_FILES or (out_bytes_total + file_size) > _OUT_MAX_BYTES:
                            omitted_count += 1
                            continue
                        with open(entry.path, "rb") as fp:
                            file_data = fp.read()
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
                return {"error": msg[:_EXEC_OUTPUT_CAP], "files": out_files}
            if stderr.strip():
                stdout = (stdout + chr(10) + "--- stderr ---" + chr(10) + stderr)[:_EXEC_OUTPUT_CAP]
            return {"result": stdout, "files": out_files}
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
            _cleanup_sandbox(protected_pids=protected_pids, kill_processes=kill_processes)
    finally:
        _EXEC_LOCK.release()


@app.post("/execute")
async def execute(req: ExecRequest):
    # Echter Sicherheitsrahmen = Container: non-root, cap_drop ALL,
    # no-new-privileges, read_only rootfs, Netz ohne Egress.
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
