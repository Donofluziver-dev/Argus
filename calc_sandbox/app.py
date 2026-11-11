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
