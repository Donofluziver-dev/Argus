#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setup3.py -- Action-Engine & Tool-Infrastructure
Zustaendigkeit: SSH-Client, Tool-Registry, Read/Write-Tools, Confirmation-Flow.
Ausfuehren nach Setup1 und Setup2.
"""

import os
from pathlib import Path

from setup_common import _log, writefile, write_env_block, prune_env_keys

try:
    from setup_common import verify_markers
except ImportError:
    verify_markers = None

BASE_DIR = Path(__file__).parent.resolve()

#--- Rechnersteuerung. "false" = nur Chat, Dokumente, Recherche: die Host-Tools
#--- werden nicht registriert, das Backend oeffnet keine SSH-Verbindung, und
#--- setup_ssh.ps1 muss nicht gelaufen sein.
ACTION_ENGINE_ENABLED = "true"


# ---------------------------------------------------------------------------
# Python-Code Templates
# ---------------------------------------------------------------------------

SSH_CLIENT_PY = r"""
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

if sys.stdout is not None and getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr is not None and getattr(sys.stderr, 'encoding', '').lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

log = logging.getLogger(__name__)

_CLIENT_KWARGS: dict[str, Any] | None = None

_SECRET_USER_PATH = Path("/run/secrets/ssh_user")
_SECRET_PASS_PATH = Path("/run/secrets/ssh_password")
#--- Public-Key-Auth ist der Normalfall. Das Passwort ist nur der Fallback
#    fuer einen Host, der noch nicht umgestellt ist.
_SECRET_KEY_PATH = Path(os.getenv("SSH_KEY_PATH", "/run/secrets/ssh_key"))

#--- Gepoolte SSH-Verbindung. _SSH_LOCK serialisiert den Zugriff.
_SSH_CLIENT = None
_SSH_LOCK = threading.Lock()
#--- TOFU-Host-Key-Pinning. known_hosts liegt im rw-Workspace, damit der Key
#    Container-Neustarts ueberlebt.
_KNOWN_HOSTS_PATH = os.getenv("SSH_KNOWN_HOSTS", "/host/argus_workspace/.argus_known_hosts")


def _sanitize_text(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("\ufeff", "").strip()


def _read_secret_file(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        return _sanitize_text(raw)
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _resolve_credential(secret_path: Path, env_key: str) -> str:
    value = _read_secret_file(secret_path)
    if value:
        return value
    return _sanitize_text(os.getenv(env_key, ""))


def _has_private_key() -> bool:
    # Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
    '''True, wenn der gemountete Key-Pfad eine nicht-leere Datei ist.

    Der Mount existiert auf einem noch nicht umgestellten Host als LEERE Datei
    (Setup1 legt sie an, damit Docker daraus kein Verzeichnis macht) -- die blosse
    Existenz sagt also nichts. Nur Groesse > 0 zaehlt als vorhandener Key.'''
    try:
        return _SECRET_KEY_PATH.is_file() and _SECRET_KEY_PATH.stat().st_size > 0
    except OSError:
        return False


def _get_client_kwargs() -> dict[str, Any]:
    global _CLIENT_KWARGS
    if _CLIENT_KWARGS is None:
        _user = _resolve_credential(_SECRET_USER_PATH, "SSH_USER")
        _pass = _resolve_credential(_SECRET_PASS_PATH, "SSH_PASSWORD")
        #--- key_filename statt selbst geladenem PKey: paramiko probiert die
        #    Key-Typen selbst durch, eine eigene Liste veraltet still.
        _key = str(_SECRET_KEY_PATH) if _has_private_key() else ""
        if not _user or not (_key or _pass):
            log.warning(
                "SSH-Credentials nicht gefunden: weder %s/%s/%s noch ENV "
                "SSH_USER/SSH_PASSWORD gesetzt.",
                _SECRET_USER_PATH, _SECRET_KEY_PATH, _SECRET_PASS_PATH,
            )
        elif _key:
            log.info("SSH-Auth: Public-Key (%s)", _SECRET_KEY_PATH)
        else:
            log.warning(
                "SSH-Auth: Passwort-Fallback -- kein Key unter %s. setup_ssh.ps1 "
                "erneut ausfuehren, um auf Public-Key umzustellen.",
                _SECRET_KEY_PATH,
            )
        _CLIENT_KWARGS = dict(
            hostname=_sanitize_text(os.getenv("SSH_HOST", "host.docker.internal")),
            username=_user,
            password=_pass,
            key_filename=_key,
            port=int(_sanitize_text(os.getenv("SSH_PORT", "22"))),
            timeout=int(_sanitize_text(os.getenv("SSH_TIMEOUT", "30"))),
        )
    return _CLIENT_KWARGS


def _connect():
    import paramiko
    kwargs = _get_client_kwargs()
    ssh = paramiko.SSHClient()
    # TOFU-Pinning: bekannte Host-Keys werden verifiziert, nur ein unbekannter
    # wird beim Erstkontakt aufgenommen.
    # Die Datei VOR dem Connect anlegen: AutoAddPolicy liest sie beim Speichern
    # und wirft sonst FileNotFoundError -- known_hosts entstuende nie.
    try:
        kh = Path(_KNOWN_HOSTS_PATH)
        kh.parent.mkdir(parents=True, exist_ok=True)
        if not kh.exists():
            kh.touch()
        ssh.load_host_keys(_KNOWN_HOSTS_PATH)
    except (FileNotFoundError, OSError) as e:
        # Ohne gesetzten _host_keys_filename kein Pinning, aber Connect klappt.
        log.warning("known_hosts nicht vorbereitbar (%s): %s", _KNOWN_HOSTS_PATH, e)
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # Leere Werte als None: paramiko prueft auf 'is not None' und versuchte sonst
    # eine Anmeldung mit LEEREM Passwort -- ein Fehlversuch gegen MaxAuthTries.
    ssh.connect(
        hostname=kwargs["hostname"],
        port=kwargs["port"],
        username=kwargs["username"],
        password=kwargs["password"] or None,
        key_filename=kwargs["key_filename"] or None,
        timeout=kwargs["timeout"],
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        Path(_KNOWN_HOSTS_PATH).parent.mkdir(parents=True, exist_ok=True)
        ssh.save_host_keys(_KNOWN_HOSTS_PATH)
    except OSError as e:
        log.warning("known_hosts nicht persistierbar (%s): %s", _KNOWN_HOSTS_PATH, e)
    return ssh


def _close_pooled():
    global _SSH_CLIENT
    if _SSH_CLIENT is not None:
        try:
            _SSH_CLIENT.close()
        except Exception:
            pass
        _SSH_CLIENT = None


def _get_pooled_client():
    global _SSH_CLIENT
    cli = _SSH_CLIENT
    if cli is not None:
        tr = cli.get_transport()
        if tr is not None and tr.is_active():
            return cli
        _close_pooled()
    _SSH_CLIENT = _connect()
    return _SSH_CLIENT


class _SSHSendError(Exception):
    # Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
    '''Fehler BEVOR das Skript vollstaendig uebergeben wurde (exec-/stdin-Phase).
    PowerShell mit "-Command -" liest stdin bis EOF, bevor es irgendetwas ausfuehrt --
    ohne abgeschlossenes shutdown_write ist also noch nichts gelaufen. NUR diese
    Phase darf nach einem Reconnect wiederholt werden.'''
    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


def _run_once(ssh, script: str, constrained: bool, timeout: int) -> dict[str, Any]:
    cmd = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -"
    # Constrained Language Mode nur fuer das generische run_powershell-Tool.
    # Ohne WDAC ist CLM nur Defense-in-Depth, KEINE Sicherheitsgrenze -- der
    # echte Schutz ist Allow-List-Gate + Bestaetigung.
    # Fest-skriptierte COM-Tools rufen mit constrained=False auf.
    if constrained:
        script = (
            "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'\n"
            + script
        )
    try:
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        stdin.write(script)
        stdin.flush()
        stdin.channel.shutdown_write()
    except Exception as e:
        raise _SSHSendError(e) from e

    # Ab hier laeuft das Skript auf dem Host -- Fehler duerfen NICHT wiederholt werden.
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return {
        "stdout": _sanitize_text(out),
        "stderr": _sanitize_text(err),
        "had_errors": exit_status != 0,
    }


def _execute_sync(script: str, constrained: bool = False, timeout: int | None = None) -> dict[str, Any]:
    import paramiko
    kwargs = _get_client_kwargs()
    # Kanal-Timeout = Tool-Timeout: exec_command(timeout=) ist ein Lese-Timeout
    # (Stille-Limit). Ein starrer Wert killt lange Kommandos ohne Output.
    channel_timeout = int(timeout) if timeout and timeout > 0 else kwargs["timeout"]
    with _SSH_LOCK:
        for attempt in (1, 2):
            #--- Phase 1: Verbindung aufbauen (kein Skript unterwegs -> Retry gefahrlos)
            try:
                ssh = _get_pooled_client()
            except paramiko.ssh_exception.BadHostKeyException as e:
                _close_pooled()
                return {
                    "stdout": "",
                    "stderr": f"SSH host key mismatch (possible MITM) -- aborted: {e}",
                    "had_errors": True,
                }
            except Exception as e:
                _close_pooled()
                if attempt == 2:
                    return {"stdout": "", "stderr": f"SSH connection error: {e}", "had_errors": True}
                continue
            #--- Phase 2: Skript uebergeben + ausfuehren
            try:
                return _run_once(ssh, script, constrained, channel_timeout)
            except _SSHSendError as e:
                # Skript kam nicht vollstaendig an -- Neuverbinden ist gefahrlos.
                _close_pooled()
                if attempt == 2:
                    return {"stdout": "", "stderr": f"SSH connection error: {e.original}", "had_errors": True}
            except Exception as e:
                # Read-Phase: das Skript LIEF bereits. Nicht-idempotente Aktionen
                # duerfen nicht blind neu gestartet werden.
                _close_pooled()
                return {
                    "stdout": "",
                    "stderr": (
                        "SSH error during execution (the command was already started, "
                        f"result unknown -- NOT retried automatically): {e}"
                    ),
                    "had_errors": True,
                }
    return {"stdout": "", "stderr": "SSH connection error: host unreachable.", "had_errors": True}


async def run_powershell(script: str, timeout: int = 30, constrained: bool = False) -> dict[str, Any]:
    try:
        # wait_for ist nur der Backstop (+10s) -- die Kanal-Fehlermeldung gewinnt.
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_sync, script, constrained, timeout),
            timeout=timeout + 10,
        )
        return result
    except asyncio.TimeoutError:
        return {
            "stdout": "",
            "stderr": (f"Timeout after {timeout}s -- the command may still be running "
                       "on the host (result unknown)."),
            "had_errors": True,
        }
    except Exception as e:
        return {"stdout": "", "stderr": _sanitize_text(str(e)), "had_errors": True}
"""

#--- Registry

REGISTRY_PY = r"""
import logging

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools = []

    def register(self, tool):
        self._tools.append(tool)
        log.info(f"Tool registriert: {getattr(tool, 'name', str(tool))}")

    def get_langchain_tools(self) -> list:
        return list(self._tools)

    def clear(self):
        self._tools.clear()


tool_registry = ToolRegistry()
"""

#--- Base-Tool

BASE_TOOL_PY = r"""
import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolClassification(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    BLOCKED = "blocked"


class ToolInput(BaseModel):
    pass


class ToolOutput(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None


#--- Akteur des laufenden Requests, fuer die HMAC-verkettete Audit-Kette.
#--- ContextVar statt globaler Variable, weil mehrere Requests nebenlaeufig
#--- im selben Event-Loop laufen.
CURRENT_USER_ID: ContextVar[int | None] = ContextVar("argus_current_user_id", default=None)

#--- Die laufende AUFGABE (ein Agenten-Turn): {'id', 'text', 'channel'}. Daran
#--- haengt, dass eine Aufgabe EINMAL gefragt wird statt bei jedem Schritt.
#--- Leer -> jede schreibende Aktion fragt einzeln (fail-safe).
CURRENT_TASK: ContextVar[dict | None] = ContextVar("argus_current_task", default=None)

#--- Ein Lock PRO AUFGABE, nicht global: sonst melden zwei Sub-Agenten derselben
#--- Aufgabe gleichzeitig die 'erste' Aktion. Der Lock wird ueber die gesamte
#--- Wartezeit gehalten -- global wuerde er unbeteiligte Aufgaben blockieren.
_TASK_GATES: dict[str, asyncio.Lock] = {}


def _task_gate(task_id: str) -> asyncio.Lock:
    lock = _TASK_GATES.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _TASK_GATES[task_id] = lock
        #--- Grob deckeln: eine Aufgabe = ein Turn.
        if len(_TASK_GATES) > 256:
            for stale in list(_TASK_GATES)[:128]:
                if not _TASK_GATES[stale].locked():
                    _TASK_GATES.pop(stale, None)
    return lock


async def _dispatch_confirmation_event(phase: str, req, **extra) -> None:
    '''Freigabe-Ereignis in den laufenden Stream geben (fuer die Chat-Karte).

    Der Blockierpunkt liegt in run() zwischen on_tool_start und on_tool_end -- dort
    streamt LangGraph von sich aus nichts, der Nutzer saehe bis zu zwei Minuten
    Stille. Ein Custom-Event schliesst die Luecke.

    Fail-open und bewusst breit gefangen: ohne Runnable-Kontext (Telegram-Pfad,
    Unit-Tests) gibt es keinen Konsumenten. Das Gate selbst funktioniert auch stumm --
    ein Ereignisproblem darf keine Freigabe verhindern.'''
    try:
        from langchain_core.callbacks.manager import adispatch_custom_event
        await adispatch_custom_event("argus_confirmation", {
            "phase": phase,
            "id": req.id,
            "tool_name": req.tool_name,
            "tool_class": req.tool_class,
            "task_scope": req.task_scope,
            "description": (req.impact_description or "")[:1500],
            "task_description": req.task_description,
            **extra,
        })
    except Exception:
        pass


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    classification: ToolClassification = ToolClassification.READ
    timeout_seconds: int = 30

    @abstractmethod
    def get_input_schema(self) -> type[BaseModel]:
        ...

    @abstractmethod
    async def _execute(self, params: BaseModel) -> ToolOutput:
        ...

    # ---- Hooks fuer Spezialisierungen (z.B. RunPowerShellTool) -------------
    # Der Audit-/Confirmation-Flow lebt EINMAL in run(); Subklassen steuern ihn
    # nur ueber diese Hooks.

    def _classify(self, params: BaseModel) -> ToolClassification:
        # Effektive Klassifizierung fuer DIESEN Aufruf (Default: statisch).
        return self.classification

    def _audit_details(self, params: BaseModel) -> str:
        # Inhalt der details-Spalte im AuditLog.
        return params.model_dump_json()

    def _veto(self, params: BaseModel, detected: ToolClassification):
        # Ablehnung VOR Confirmation/Ausfuehrung: (audit_status, fehlertext) oder None.
        return None

    # ---- Freigabe-Gate ----------------------------------------------------

    def _task_rejected(self, _audit) -> ToolOutput:
        '''Antwort, wenn der Nutzer die ganze Aufgabe abgelehnt hat.

        Der Text ist bewusst eine Anweisung an das Modell: ohne sie sucht es nach
        einem Umweg, statt zu stoppen -- und genau das soll eine Ablehnung verhindern.'''
        _audit("confirmation_task_rejected")
        return ToolOutput(
            success=False,
            error=(
                "Action not confirmed: the user rejected this task. Do NOT retry and do "
                "NOT look for a workaround. Stop, summarize what you had planned, and ask "
                "how to proceed."
            ),
        )

    async def _run_gate(self, validated, params, detected, user_id,
                        task: dict, task_scope: bool, _audit):
        '''Freigabe einholen. Liefert None (freigegeben) oder die Absage als ToolOutput.'''
        from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
        # Import im Funktionskoerper: so greift das Monkeypatching der Tests.
        from rag_backend.crypto_utils import load_secret
        # Ein Kanal genuegt: Telegram ODER eine offene Chat-Karte. Der OpenAI-Proxy
        # kann keine Karte zeigen -- dort bleibt Telegram Pflicht.
        has_telegram = bool(load_secret("TELEGRAM_BOT_TOKEN")) and bool(load_secret("TELEGRAM_CHAT_ID"))
        has_chat = task.get("channel") == "dashboard"
        if not has_telegram and not has_chat:
            _audit("confirmation_no_channel")
            return ToolOutput(
                success=False,
                error=(
                    "Operation blocked: no confirmation channel configured. "
                    "Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in "
                    "API_Tokens/telegram.txt, or run the task from the Argus chat."
                ),
            )
        impact = self._impact_description(validated)
        if task_scope:
            impact = (
                f"AUFGABE: {task.get('text') or '(ohne Beschreibung)'}\n"
                "Diese Freigabe erlaubt ALLE schreibenden Schritte dieser Aufgabe. "
                "Loeschende Aktionen fragen weiterhin einzeln nach.\n\n"
                f"Erster Schritt:\n{impact}"
            )
        req = confirmation_store.create(
            tool_name=self.name,
            tool_class=detected.value,
            parameters=params,
            impact_description=impact,
            # Urheber mitschreiben: ueber HTTP darf nur er selbst freigeben.
            requested_by=user_id,
            task_id=task.get("id"),
            task_scope=task_scope,
            task_description=(task.get("text") or ""),
        )
        await _dispatch_confirmation_event("pending", req)
        resolved = await confirmation_store.wait_for_resolution(req.id)
        status_text = resolved.status.value if resolved else "timeout"
        # Auch der Ausgang geht an die Karte, sonst bleibt sie bis zum Neuladen offen.
        await _dispatch_confirmation_event("resolved", req, status=status_text)
        if not resolved or resolved.status != ConfirmationStatus.APPROVED:
            _audit(f"confirmation_{status_text}")
            return ToolOutput(
                success=False,
                error=f"Action not confirmed (status: {status_text}).",
            )
        return None

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        return detected in {ToolClassification.WRITE, ToolClassification.DESTRUCTIVE}

    def requires_confirmation(self) -> bool:
        return self._requires_confirmation(self.classification)

    async def run(self, params: dict, user_id: int | None = None) -> ToolOutput:
        from rag_backend.action_engine.audit import write_audit_log
        from rag_backend.crypto_utils import load_secret

        input_schema = self.get_input_schema()
        validated = input_schema(**params)
        input_hash = hashlib.sha256(validated.model_dump_json().encode()).hexdigest()[:16]
        detected = self._classify(validated)
        details = self._audit_details(validated)

        def _audit(status: str, output_hash: str | None = None, latency_ms: int = 0):
            write_audit_log(
                tool=self.name,
                tool_class=detected.value,
                input_hash=input_hash,
                output_hash=output_hash,
                status=status,
                latency_ms=latency_ms,
                user_id=user_id,
                details=details,
            )

        veto = self._veto(validated, detected)
        if veto:
            veto_status, veto_error = veto
            _audit(veto_status)
            return ToolOutput(success=False, error=veto_error)

        if self._requires_confirmation(detected):
            task = CURRENT_TASK.get() or {}
            task_id = task.get("id")
            #--- LOESCHENDE Aktionen bleiben immer einzeln bestaetigungspflichtig,
            #--- auch innerhalb einer freigegebenen Aufgabe. Das begrenzt, was eine
            #--- per Prompt-Injection gekaperte Aufgabe anrichten kann.
            is_destructive = detected == ToolClassification.DESTRUCTIVE
            #--- Aufgabenweite Freigabe nur, wenn es ueberhaupt eine Aufgabe gibt.
            task_scope = bool(task_id) and not is_destructive

            if task_scope:
                from rag_backend.action_engine.confirmation import confirmation_store
                prior = confirmation_store.task_status(task_id)
                if prior == "rejected":
                    return self._task_rejected(_audit)
                if prior == "approved":
                    # Eigener Audit-Status, damit im Log sichtbar bleibt, WARUM
                    # hier nicht gefragt wurde.
                    _audit("task_approved_skip")
                else:
                    #--- Im Aufgaben-Lock nachfassen: ein paralleler Sub-Agent
                    #--- kann inzwischen gefragt haben.
                    async with _task_gate(task_id):
                        prior = confirmation_store.task_status(task_id)
                        if prior == "rejected":
                            return self._task_rejected(_audit)
                        if prior == "approved":
                            _audit("task_approved_skip")
                        else:
                            blocked = await self._run_gate(
                                validated, params, detected, user_id, task, task_scope, _audit)
                            if blocked is not None:
                                return blocked
            else:
                blocked = await self._run_gate(
                    validated, params, detected, user_id, task, task_scope, _audit)
                if blocked is not None:
                    return blocked

        t0 = time.monotonic()
        try:
            result = await self._execute(validated)
            latency_ms = int((time.monotonic() - t0) * 1000)
            output_hash = hashlib.sha256(
                result.model_dump_json().encode()
            ).hexdigest()[:16] if result.data else None
            _audit("ok" if result.success else "error", output_hash=output_hash, latency_ms=latency_ms)
            return result
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _audit("exception", latency_ms=latency_ms)
            return ToolOutput(success=False, error=str(e))

    def _impact_description(self, params: BaseModel) -> str:
        return f"{self.name} mit Parametern: {params.model_dump_json()}"

    def to_langchain_tool(self):
        from langchain_core.tools import StructuredTool

        input_schema = self.get_input_schema()
        tool_instance = self

        async def _afunc(**kwargs) -> str:
            result = await tool_instance.run(kwargs, user_id=CURRENT_USER_ID.get())
            if result.success:
                return str(result.data)
            #--- Teil-Ausgaben ueberleben den Fehlerfall: run_powershell fuellt bei
            #--- had_errors stdout UND stderr, und genau das stdout sagt dem Agenten,
            #--- wie weit das Skript kam. Das "Error:"-Praefix bleibt vorn -- daran
            #--- erkennt der Graph den Fehlversuch (_is_failed_tool_result).
            if result.data:
                return f"Error: {result.error}\n--- Partial output ---\n{result.data}"
            return f"Error: {result.error}"

        return StructuredTool.from_function(
            coroutine=_afunc,
            name=self.name,
            description=self.description,
            args_schema=input_schema,
        )
"""

#--- Audit

AUDIT_PY = r"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_HMAC_KEY: bytes | None = None


def _get_hmac_key() -> bytes:
    global _HMAC_KEY
    if _HMAC_KEY is None:
        # Dedizierter HMAC-Key, kein Rueckfall auf den Fernet-Key: der ist
        # welt-lesbar und mehrfach verwendet.
        from rag_backend.crypto_utils import load_secret
        key = load_secret("AUDIT_HMAC_KEY")
        if not key:
            raise RuntimeError(
                "AUDIT_HMAC_KEY nicht verfuegbar -- Setup1.py erneut ausfuehren "
                "(legt API_Tokens/audit_hmac_key.txt an)."
            )
        _HMAC_KEY = key.encode()
    return _HMAC_KEY


def _compute_hmac(payload: dict) -> str:
    key = _get_hmac_key()
    msg = json.dumps(payload, sort_keys=True, default=str).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def write_audit_log(
    tool: str,
    tool_class: str,
    input_hash: str,
    output_hash: str | None,
    status: str,
    latency_ms: int | None = None,
    user_id: int | None = None,
    details: str | None = None,
):
    try:
        from rag_backend.database import SessionLocal
        from rag_backend.models import AuditLog

        # ts als naive-UTC: die Spalte ist tz-naiv, sonst bricht +00:00 die Pruefung.
        ts = datetime.now(timezone.utc).replace(tzinfo=None)

        with SessionLocal() as db:
            # Hash-Chain: die Signatur der letzten Zeile geht in den Payload dieser
            # Zeile ein. Aendern/Loeschen/Umsortieren bricht die Kette.
            # Advisory-Lock, weil Hauptprozess und MCP-Subprozess in dieselbe Kette
            # schreiben; SQLite kennt die Funktion nicht -> still ignorieren.
            from sqlalchemy import text as _sa_text
            try:
                db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"), {"k": 0x4155444954})
            except Exception:
                pass
            last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
            prev_sig = last.hmac_signature if (last and last.hmac_signature) else ""

            payload = {
                "ts": ts.isoformat(timespec="microseconds"),
                "tool": tool,
                "tool_class": tool_class,
                "input_hash": input_hash,
                "output_hash": output_hash or "",
                "status": status,
                "latency_ms": latency_ms or 0,
                "user_id": user_id or 0,
                "details": details or "",
                "prev": prev_sig,
            }
            signature = _compute_hmac(payload)

            db.add(AuditLog(
                ts=ts,
                user_id=user_id,
                tool=tool,
                tool_class=tool_class,
                input_hash=input_hash,
                output_hash=output_hash,
                status=status,
                latency_ms=latency_ms,
                hmac_signature=signature,
                details=details,
            ))
            db.commit()

        # Zusaetzlich als JSONL auf dem Host-Volume, ein File pro Tag.
        try:
            import os, pathlib
            audit_dir = pathlib.Path("/app/evals/Action_Audit")
            audit_dir.mkdir(parents=True, exist_ok=True)
            daily_file = audit_dir / f"audit_{ts.strftime('%Y-%m-%d')}.jsonl"
            line = json.dumps({
                "ts": ts.isoformat(timespec="microseconds"),
                "tool": tool, "tool_class": tool_class,
                "status": status, "latency_ms": latency_ms,
                "input_hash": input_hash, "output_hash": output_hash,
                "user_id": user_id, "details": details,
                "hmac": signature,
            }, ensure_ascii=False)
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e2:
            log.warning(f"audit_log Datei-Persistierung fehlgeschlagen: {e2}")
    except Exception as e:
        log.warning(f"audit_log Schreibfehler: {e}")


def verify_audit_chain() -> dict:
    # Dreifach-Single-Quotes: dieses Template ist mit Double-Quotes begrenzt.
    '''Rechnet die HMAC-Hash-Chain ueber alle Audit-Zeilen (id aufsteigend) neu und
    meldet, ob die Kette intakt ist: {ok, checked, broken_id}. broken_id ist die
    erste Zeile, deren Signatur sich nicht reproduzieren laesst (Manipulation/Luecke).
    Hinweis: Zeilen, die VOR Einfuehrung der chained-HMAC geschrieben wurden, zaehlen
    als gebrochen -- nach einem Clean-Rebuild der DB faengt die Kette sauber an.'''
    try:
        from rag_backend.database import SessionLocal
        from rag_backend.models import AuditLog
    except Exception as e:
        return {"ok": False, "checked": 0, "broken_id": None, "error": str(e)}
    prev_sig = ""
    checked = 0
    with SessionLocal() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        for r in rows:
            payload = {
                "ts": r.ts.isoformat(timespec="microseconds") if r.ts else "",
                "tool": r.tool,
                "tool_class": r.tool_class,
                "input_hash": r.input_hash,
                "output_hash": r.output_hash or "",
                "status": r.status,
                "latency_ms": r.latency_ms or 0,
                "user_id": r.user_id or 0,
                "details": r.details or "",
                "prev": prev_sig,
            }
            if _compute_hmac(payload) != r.hmac_signature:
                return {"ok": False, "checked": checked, "broken_id": r.id}
            prev_sig = r.hmac_signature
            checked += 1
    return {"ok": True, "checked": checked, "broken_id": None}
"""

#--- Confirmation

CONFIRMATION_PY = r"""
import asyncio
import json
import logging
import os
import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)


class ConfirmationStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    COOLDOWN = "cooldown"
    TIMEOUT = "timeout"


class ConfirmationRequest(BaseModel):
    id: str
    tool_name: str
    tool_class: str
    parameters: dict
    impact_description: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    created_at: float = 0.0
    cooldown_started_at: float = 0.0
    telegram_message_id: Optional[int] = None
    telegram_chat_id: Optional[int] = None
    #--- Wer die Aktion ausgeloest hat. Ueber HTTP darf NUR dieser Akteur sie
    #--- freigeben. Telegram bleibt unberuehrt -- dort ist die Chat-ID die Auth.
    requested_by: Optional[int] = None
    #--- Aufgaben-Freigabe: eine Aufgabe = ein Agenten-Turn. Die ERSTE schreibende
    #--- Aktion fragt fuer das ganze Vorhaben. task_scope bleibt bei LOESCHENDEN
    #--- Aktionen False -- die fragen immer einzeln.
    task_id: Optional[str] = None
    task_scope: bool = False
    task_description: str = ""


#--- Besitzer-Kanal (Telegram): dort ist die Chat-ID-Pruefung die Authentifizierung.
#--- Der HTTP-Weg uebergibt immer die echte User-ID des Aufrufers.
OWNER_CHANNEL = -1


def _actor_may_resolve(req: "ConfirmationRequest", actor: Optional[int]) -> bool:
    '''Darf dieser Akteur die Confirmation aufloesen (freigeben/ablehnen/sehen)?

    Ohne bekannten Urheber bleibt nur der Besitzer-Kanal -- ueber HTTP ist eine
    Anfrage ohne requested_by nicht freigebbar (fail-closed).'''
    if actor == OWNER_CHANNEL:
        return True
    if req.requested_by is None:
        return False
    return actor == req.requested_by


STATE_FILE = "/tmp/confirmations.json"
LOCK_FILE = "/tmp/confirmations.lock"
#--- Aufgaben-Freigaben liegen in einer EIGENEN Datei: die Leser von
#--- confirmations.json parsen jeden Wert als ConfirmationRequest und wuerden
#--- an einem fremdgeformten Eintrag zerbrechen. Derselbe FileLock fuer beide.
TASKS_FILE = "/tmp/task_approvals.json"


class FileLock:
    def __init__(self, path=LOCK_FILE):
        self.path = path
        self.fd = None

    def __enter__(self):
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 60.0:
                        os.unlink(self.path)   # verwaisten Lock uebernehmen
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            os.close(self.fd)
            try:
                os.unlink(self.path)
            except Exception:
                pass


def _load_state() -> dict[str, dict]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict[str, dict]):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Failed to save confirmation state: {e}")


def _load_tasks() -> dict[str, dict]:
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tasks(tasks: dict[str, dict]):
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Failed to save task approvals: {e}")


class ConfirmationStore:
    def __init__(self, timeout_seconds: int = 120, cooldown_seconds: Optional[int] = None):
        self._timeout = int(os.getenv("CONFIRMATION_TIMEOUT_SECONDS", str(timeout_seconds)))
        if cooldown_seconds is None:
            cooldown_seconds = int(os.getenv("CONFIRMATION_COOLDOWN_SECONDS", "30"))
        self._cooldown = cooldown_seconds
        #--- Zeitdeckel der Aufgaben-Freigabe -- Sicherheitsnetz fuer einen
        #--- haengenden Turn, nicht die normale Lebensdauer.
        self._task_ttl = int(os.getenv("TASK_APPROVAL_TTL_SECONDS", "1800"))
        self._cleanup_task: Optional[asyncio.Task] = None

    #--- Aufgaben-Freigabe (Auto-Modus) ---------------------------------------

    def task_status(self, task_id: Optional[str]) -> Optional[str]:
        '''"approved" | "rejected" | None fuer diese Aufgabe.

        Raeumt abgelaufene Eintraege gleich mit weg: die Freigabe soll nach dem
        Zeitdeckel wirklich verschwinden und nicht nur ignoriert werden.'''
        if not task_id:
            return None
        with FileLock():
            tasks = _load_tasks()
            now = time.time()
            changed = False
            for k, v in list(tasks.items()):
                if now > v.get("expires_at", 0.0):
                    tasks.pop(k, None)
                    changed = True
            entry = tasks.get(task_id)
            if changed:
                _save_tasks(tasks)
        return entry.get("status") if entry else None

    def _mark_task(self, req: "ConfirmationRequest", status: str) -> None:
        '''Ergebnis einer aufgabenweiten Freigabe festhalten.

        EINZIGER Schreibort -- aufgerufen aus approve()/reject(). Dadurch wirkt es
        identisch, egal ob die Entscheidung ueber die Chat-Karte, Telegram oder den
        Login-Endpunkt kam; kein Aufrufer muss etwas zusaetzlich tun.'''
        if not (req.task_scope and req.task_id):
            return
        with FileLock():
            tasks = _load_tasks()
            tasks[req.task_id] = {
                "status": status,
                "resolved_at": time.time(),
                "expires_at": time.time() + self._task_ttl,
                "requested_by": req.requested_by,
                "task_description": req.task_description,
            }
            _save_tasks(tasks)
        log.info(f"Aufgabe {req.task_id}: {status} (gilt {self._task_ttl}s).")

    def _pop_expired(self) -> list[ConfirmationRequest]:
        # Entfernt abgelaufene Requests und gibt sie mit Status TIMEOUT zurueck.
        expired: list[ConfirmationRequest] = []
        with FileLock():
            state = _load_state()
            now = time.time()
            for k, v in list(state.items()):
                created_at = v.get("created_at", 0.0)
                if now - created_at > self._timeout:
                    v["status"] = ConfirmationStatus.TIMEOUT.value
                    expired.append(ConfirmationRequest(**v))
                    state.pop(k, None)
            if expired:
                _save_state(state)
        return expired

    def _clean_expired(self):
        for req in self._pop_expired():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._notify_timeout_telegram(req))
            except RuntimeError:
                pass

    async def _clean_expired_async(self):
        for req in self._pop_expired():
            await self._notify_timeout_telegram(req)

    async def _background_cleanup_loop(self):
        log.info("ConfirmationStore: Background TTL cleanup loop started.")
        while True:
            try:
                await asyncio.sleep(30)
                await self._clean_expired_async()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Error in background cleanup loop: {e}")

    def create(
        self,
        tool_name: str,
        tool_class: str,
        parameters: dict,
        impact_description: str,
        requested_by: Optional[int] = None,
        task_id: Optional[str] = None,
        task_scope: bool = False,
        task_description: str = "",
    ) -> ConfirmationRequest:
        self._clean_expired()
        try:
            loop = asyncio.get_running_loop()
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = loop.create_task(self._background_cleanup_loop())
        except RuntimeError:
            pass
        req = ConfirmationRequest(
            id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            tool_class=tool_class,
            parameters=parameters,
            impact_description=impact_description,
            created_at=time.time(),
            requested_by=requested_by,
            task_id=task_id,
            task_scope=task_scope,
            task_description=task_description,
        )
        with FileLock():
            state = _load_state()
            state[req.id] = json.loads(req.model_dump_json())
            _save_state(state)

        log.info(f"Confirmation erstellt: {req.id} fuer {tool_name}")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify_telegram(req))
        except RuntimeError:
            pass
        return req

    async def _notify_telegram(self, req: ConfirmationRequest):
        from rag_backend.crypto_utils import load_secret
        if not load_secret("TELEGRAM_BOT_TOKEN") or not load_secret("TELEGRAM_CHAT_ID"):
            return
        try:
            from rag_backend.telegram_bot import send_confirmation_request
            msg = await send_confirmation_request(
                confirmation_id=req.id,
                tool_name=req.tool_name,
                tool_class=req.tool_class,
                impact_description=req.impact_description,
            )
            if msg:
                with FileLock():
                    state = _load_state()
                    if req.id in state:
                        state[req.id]["telegram_message_id"] = msg.message_id
                        state[req.id]["telegram_chat_id"] = msg.chat.id
                        _save_state(state)
        except Exception as e:
            log.warning(f"Telegram-Notification fehlgeschlagen: {e}")

    async def _notify_timeout_telegram(self, req: ConfirmationRequest):
        if req.telegram_chat_id and req.telegram_message_id:
            try:
                from rag_backend.telegram_bot import edit_message_to_timeout
                await edit_message_to_timeout(
                    chat_id=req.telegram_chat_id,
                    message_id=req.telegram_message_id,
                    tool_name=req.tool_name,
                )
            except Exception as e:
                log.warning(f"Failed to send timeout notification to Telegram: {e}")

    def list_pending(self, actor: Optional[int] = OWNER_CHANNEL) -> list[ConfirmationRequest]:
        self._clean_expired()
        with FileLock():
            state = _load_state()
        pending = []
        for v in state.values():
            req = ConfirmationRequest(**v)
            if req.status not in {ConfirmationStatus.PENDING, ConfirmationStatus.COOLDOWN}:
                continue
            # Fremde Anfragen nicht zeigen -- die ID ist der Schluessel zur Freigabe.
            if not _actor_may_resolve(req, actor):
                continue
            pending.append(req)
        return pending

    def _audit_decision(self, req: "ConfirmationRequest", status: str,
                        actor: Optional[int], via: str) -> None:
        '''Jede Freigabe-Entscheidung ins Audit-Log.

        Vorher fehlte das komplett: eine genehmigte Aktion erschien nur als "ok" und
        war von einer, die nie eine Freigabe brauchte, nicht zu unterscheiden -- wer
        wann was freigegeben hat, stand nirgends. Genau das ist aber die Frage, die
        man nach einem Vorfall stellt. Fail-open: ein Audit-Problem darf eine bereits
        getroffene Entscheidung nicht zurueckdrehen.'''
        try:
            from rag_backend.action_engine.audit import write_audit_log
            write_audit_log(
                tool="confirmation",
                tool_class=req.tool_class,
                input_hash=req.id,
                output_hash=None,
                status=status,
                latency_ms=0,
                user_id=(actor if actor != OWNER_CHANNEL else None),
                details=json.dumps({
                    "id": req.id, "tool_name": req.tool_name,
                    "task_scope": req.task_scope, "task_id": req.task_id,
                    "via": via,
                }, ensure_ascii=False),
            )
        except Exception as e:
            log.warning(f"Audit der Freigabe-Entscheidung fehlgeschlagen: {e}")

    def approve(self, confirmation_id: str,
                actor: Optional[int] = OWNER_CHANNEL,
                via: str = "http") -> Optional[ConfirmationRequest]:
        self._clean_expired()
        with FileLock():
            state = _load_state()
            req_dict = state.get(confirmation_id)
            if not req_dict:
                return None
            req = ConfirmationRequest(**req_dict)
            if not _actor_may_resolve(req, actor):
                # Wie 'nicht gefunden' behandeln: eine eigene Meldung wuerde die
                # Existenz bestaetigen.
                return None
            if req.status in {ConfirmationStatus.APPROVED, ConfirmationStatus.COOLDOWN}:
                return req
            if req.tool_class == "destructive":
                req.status = ConfirmationStatus.COOLDOWN
                req.cooldown_started_at = time.time()
            else:
                req.status = ConfirmationStatus.APPROVED
            state[confirmation_id] = json.loads(req.model_dump_json())
            _save_state(state)
        # Ausserhalb des Locks: _mark_task nimmt ihn selbst.
        if req.status == ConfirmationStatus.APPROVED:
            self._mark_task(req, "approved")
            self._audit_decision(req, "approved", actor, via)
        else:
            self._audit_decision(req, "cooldown_started", actor, via)
        return req

    def confirm_after_cooldown(self, confirmation_id: str,
                               actor: Optional[int] = OWNER_CHANNEL,
                               via: str = "http") -> tuple[Optional[ConfirmationRequest], float]:
        self._clean_expired()
        with FileLock():
            state = _load_state()
            req_dict = state.get(confirmation_id)
            if not req_dict:
                return None, 0.0
            req = ConfirmationRequest(**req_dict)
            if not _actor_may_resolve(req, actor):
                return None, 0.0
            if req.status != ConfirmationStatus.COOLDOWN:
                return None, 0.0
            elapsed = time.time() - req.cooldown_started_at
            if elapsed < self._cooldown:
                return None, self._cooldown - elapsed
            req.status = ConfirmationStatus.APPROVED
            state[confirmation_id] = json.loads(req.model_dump_json())
            _save_state(state)
        # Bei loeschenden Aktionen ist task_scope False, _mark_task tut nichts.
        # Der Aufruf steht trotzdem da, damit ein spaeterer WRITE-Cooldown-Fall
        # nicht still an der Markierung vorbeilaeuft.
        self._mark_task(req, "approved")
        self._audit_decision(req, "approved", actor, via)
        return req, 0.0

    def reject(self, confirmation_id: str,
               actor: Optional[int] = OWNER_CHANNEL,
               via: str = "http") -> Optional[ConfirmationRequest]:
        self._clean_expired()
        with FileLock():
            state = _load_state()
            req_dict = state.get(confirmation_id)
            if not req_dict:
                return None
            req = ConfirmationRequest(**req_dict)
            if not _actor_may_resolve(req, actor):
                return None
            req.status = ConfirmationStatus.REJECTED
            state[confirmation_id] = json.loads(req.model_dump_json())
            _save_state(state)
        # Ablehnung merken: sonst erzeugt die naechste schreibende Aktion derselben
        # Aufgabe sofort die naechste Karte.
        self._mark_task(req, "rejected")
        self._audit_decision(req, "rejected", actor, via)
        return req

    async def wait_for_resolution(self, confirmation_id: str) -> Optional[ConfirmationRequest]:
        start_time = time.time()
        while time.time() - start_time < self._timeout + 5:
            with FileLock():
                state = _load_state()
                req_dict = state.get(confirmation_id)

            if not req_dict:
                return None

            req = ConfirmationRequest(**req_dict)
            if req.status in {ConfirmationStatus.APPROVED, ConfirmationStatus.REJECTED, ConfirmationStatus.TIMEOUT}:
                with FileLock():
                    state = _load_state()
                    state.pop(confirmation_id, None)
                    _save_state(state)
                return req

            await asyncio.sleep(0.5)

        # Timeout
        timed_out = None
        with FileLock():
            state = _load_state()
            req_dict = state.get(confirmation_id)
            if req_dict:
                timed_out = ConfirmationRequest(**req_dict)
                timed_out.status = ConfirmationStatus.TIMEOUT
                state.pop(confirmation_id, None)
                _save_state(state)
        if timed_out is not None:
            await self._notify_timeout_telegram(timed_out)
            # Auch das Verstreichenlassen ist eine Entscheidung und gehoert ins Log.
            self._audit_decision(timed_out, "timeout", None, "expired")
            return timed_out
        return None


confirmation_store = ConfirmationStore()
"""

#--- action_engine/__init__.py

AE_INIT_PY = r"""
from rag_backend.action_engine.registry import ToolRegistry, tool_registry
from rag_backend.action_engine.base_tool import BaseTool, ToolClassification
from rag_backend.action_engine.audit import write_audit_log
"""

#--- Read-Tools

DISK_SPACE_PY = r"""
from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell


class DiskSpaceInput(BaseModel):
    drive: str = Field(
        default="C",
        pattern=r"^[A-Za-z]:?\\?$",
        description="Drive letter (e.g. 'C', 'D')",
    )


class CheckDiskSpaceTool(BaseTool):
    name = "check_disk_space"
    description = "Checks the free disk space on a Windows drive."
    classification = ToolClassification.READ
    timeout_seconds = 15

    def get_input_schema(self) -> type[BaseModel]:
        return DiskSpaceInput

    async def _execute(self, params: DiskSpaceInput) -> ToolOutput:
        script = (
            f"Get-PSDrive {params.drive[0]} "
            f"| Select-Object @{{N='Used_GB';E={{[math]::Round($_.Used/1GB,2)}}}},"
            f"@{{N='Free_GB';E={{[math]::Round($_.Free/1GB,2)}}}} "
            f"| ConvertTo-Json"
        )
        result = await run_powershell(script, timeout=self.timeout_seconds)
        if result["had_errors"]:
            return ToolOutput(success=False, error=result["stderr"])
        return ToolOutput(success=True, data=result["stdout"])
"""

WINDOWS_UPDATES_PY = r"""
from pydantic import BaseModel

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell


class WindowsUpdatesInput(BaseModel):
    pass


class GetWindowsUpdatesTool(BaseTool):
    name = "get_windows_updates"
    description = "Checks for pending Windows updates."
    classification = ToolClassification.READ
    # Die COM-Update-Suche arbeitet oft 1-3 Minuten ohne Output.
    timeout_seconds = 180

    def get_input_schema(self) -> type[BaseModel]:
        return WindowsUpdatesInput

    async def _execute(self, params: WindowsUpdatesInput) -> ToolOutput:
        script = (
            "$Session = New-Object -ComObject Microsoft.Update.Session; "
            "$Searcher = $Session.CreateUpdateSearcher(); "
            "$Results = $Searcher.Search('IsInstalled=0'); "
            "if ($Results.Updates.Count -eq 0) { 'Keine ausstehenden Updates.' } "
            "else { $Results.Updates | ForEach-Object { "
            "  [PSCustomObject]@{Title=$_.Title; KB=($_.KBArticleIDs -join ','); "
            "  Size=[math]::Round($_.MaxDownloadSize/1MB,1)} "
            "} | ConvertTo-Json -Depth 2 }"
        )
        result = await run_powershell(script, timeout=self.timeout_seconds)
        if result["had_errors"] and not result["stdout"].strip():
            return ToolOutput(success=False, error=result["stderr"])
        data = result["stdout"]
        if result["had_errors"]:
            # Teilerfolg nicht als sauberen Erfolg maskieren: die Warnung geht als
            # Teil der Daten ans Modell.
            data += "\n[WARNING: PowerShell additionally reported errors: " + ((result["stderr"] or "unknown")[:300]) + "]"
        return ToolOutput(success=True, data=data)
"""

EVENTLOG_PY = r"""
from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell


class EventlogInput(BaseModel):
    log_name: str = Field(
        default="System",
        description="Name of the event log (e.g. 'System', 'Application', 'Security')",
    )
    level: int = Field(
        default=2,
        description="Minimum level: 1=Critical, 2=Error, 3=Warning, 4=Information",
        ge=1,
        le=4,
    )
    max_events: int = Field(
        default=20,
        description="Maximum number of returned entries",
        ge=1,
        le=100,
    )


class ReadEventlogTool(BaseTool):
    name = "read_eventlog"
    description = "Reads the latest event log entries of the local Windows system, filtered by log name and severity."
    classification = ToolClassification.READ
    timeout_seconds = 20

    def get_input_schema(self) -> type[BaseModel]:
        return EventlogInput

    _ALLOWED_LOGS = frozenset({"System", "Application", "Security", "Setup", "ForwardedEvents"})

    async def _execute(self, params: EventlogInput) -> ToolOutput:
        if params.log_name not in self._ALLOWED_LOGS:
            return ToolOutput(success=False, error=f"Invalid log name '{params.log_name}'. Allowed: {sorted(self._ALLOWED_LOGS)}")
        script = (
            f"Get-WinEvent -FilterHashtable @{{"
            f"LogName='{params.log_name}'; Level={params.level}"
            f"}} -MaxEvents {params.max_events} -ErrorAction SilentlyContinue "
            f"| Select-Object TimeCreated, Id, LevelDisplayName, Message "
            f"| ConvertTo-Json -Depth 3"
        )
        result = await run_powershell(script, timeout=self.timeout_seconds)
        if result["had_errors"] and not result["stdout"].strip():
            return ToolOutput(success=False, error=result["stderr"] or "No entries found.")
        output = result["stdout"] or "No entries found."
        if result["had_errors"]:
            # Wie bei get_windows_updates: Output + Fehler = Teilerfolg, Warnung sichtbar machen.
            output += "\n[WARNING: PowerShell additionally reported errors: " + ((result["stderr"] or "unknown")[:300]) + "]"
        return ToolOutput(success=True, data=output)
"""

#--- Write-Tools

WINDOWS_UPDATE_INSTALL_PY = r"""
from pydantic import BaseModel

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell


class WindowsUpdateInstallInput(BaseModel):
    pass


class InstallWindowsUpdatesTool(BaseTool):
    name = "install_windows_updates"
    description = "Installs all pending Windows updates. Requires double confirmation with cool-down."
    classification = ToolClassification.DESTRUCTIVE
    timeout_seconds = 600

    def get_input_schema(self) -> type[BaseModel]:
        return WindowsUpdateInstallInput

    async def _execute(self, params: WindowsUpdateInstallInput) -> ToolOutput:
        script = (
            "$Session = New-Object -ComObject Microsoft.Update.Session; "
            "$Searcher = $Session.CreateUpdateSearcher(); "
            "$Results = $Searcher.Search('IsInstalled=0'); "
            "if ($Results.Updates.Count -eq 0) { "
            "  'No pending updates.' "
            "} else { "
            "  $Downloader = $Session.CreateUpdateDownloader(); "
            "  $Downloader.Updates = $Results.Updates; "
            "  $Downloader.Download() | Out-Null; "
            "  $Installer = $Session.CreateUpdateInstaller(); "
            "  $Installer.Updates = $Results.Updates; "
            "  $InstallResult = $Installer.Install(); "
            "  [PSCustomObject]@{ "
            "    ResultCode = $InstallResult.ResultCode; "
            "    RebootRequired = $InstallResult.RebootRequired; "
            "    InstalledCount = $Results.Updates.Count "
            "  } | ConvertTo-Json "
            "}"
        )
        result = await run_powershell(script, timeout=self.timeout_seconds)
        if result["had_errors"]:
            return ToolOutput(success=False, error=result["stderr"])
        return ToolOutput(success=True, data=result["stdout"])

    def _impact_description(self, params: WindowsUpdateInstallInput) -> str:
        return "Alle ausstehenden Windows Updates herunterladen und installieren. Neustart kann erforderlich sein."
"""

RESTART_SERVICE_PY = r"""
from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell

_SERVICE_ALLOWLIST = {
    "Spooler",
    "wuauserv",
    "BITS",
    "WinRM",
    "Dnscache",
    "LanmanWorkstation",
    "LanmanServer",
    "W32Time",
    "Themes",
    "AudioSrv",
}


class RestartServiceInput(BaseModel):
    service_name: str = Field(
        description="Windows service name (e.g. 'Spooler', 'wuauserv'). Must be on the allowlist.",
    )


class RestartServiceTool(BaseTool):
    name = "restart_service"
    description = "Restarts a Windows service. Only services on the allowlist are permitted. Requires confirmation."
    classification = ToolClassification.WRITE
    timeout_seconds = 30

    def get_input_schema(self) -> type[BaseModel]:
        return RestartServiceInput

    async def _execute(self, params: RestartServiceInput) -> ToolOutput:
        if params.service_name not in _SERVICE_ALLOWLIST:
            return ToolOutput(
                success=False,
                error=f"Service '{params.service_name}' is not on the allowlist. "
                      f"Allowed: {', '.join(sorted(_SERVICE_ALLOWLIST))}",
            )
        script = (
            f"Restart-Service -Name '{params.service_name}' -Force -PassThru "
            f"| Select-Object Name, Status, StartType "
            f"| ConvertTo-Json"
        )
        result = await run_powershell(script, timeout=self.timeout_seconds)
        if result["had_errors"]:
            return ToolOutput(success=False, error=result["stderr"])
        return ToolOutput(success=True, data=result["stdout"])

    def _impact_description(self, params: RestartServiceInput) -> str:
        return f"Windows-Dienst '{params.service_name}' neu starten"
"""

#--- Generisches PowerShell-Tool mit dynamischer Klassifizierung

RUN_POWERSHELL_TOOL_PY = r"""
import os
import re
from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput
from rag_backend.action_engine.ssh_client import run_powershell

_BLOCKED_PATTERNS = re.compile(
    r"(?:\b("
    r"Format-Volume|Stop-Computer|Restart-Computer|"
    r"Clear-RecycleBin|Reset-Computer|"
    r"Disable-NetAdapter|Remove-Partition|"
    r"Initialize-Disk|Clear-Disk|Set-ExecutionPolicy|"
    r"LanguageMode"  # Revert aus ConstrainedLanguage verhindern
    r")\b)"
    r"|__PSLockdownPolicy"
    # Lesezugriffe auf Secrets/Keys sind keine harmlosen READs -> blockieren.
    # Auch Host-Geheimnisse (SSH-Keys, Zertifikate, .env, .ssh).
    r"|(?:ssh_password|ssh_user|ssh_key|fernet_key|postgres_password|jwt_secret|"
    r"webui_secret_key|sglang_api_key|audit_hmac_key|telegram\.txt|"
    r"/run/secrets|API_Tokens|"
    r"id_rsa|id_ed25519|id_ecdsa|\.ssh[\\/]|"
    # Passwort-Tresore/Browser-Logins: auch als getarnter READ nicht abziehbar.
    r"\.kdbx\b|\.kdb\b|Login Data|"
    r"\.pem\b|\.pfx\b|\.p12\b|\.ppk\b|\.env\b)",
    re.IGNORECASE,
)

_DESTRUCTIVE_PATTERNS = re.compile(
    r"(?:\b("
    r"Remove-[\w-]+|"
    r"Uninstall-[\w-]+|"
    r"Clear-Content|Clear-Item|Clear-EventLog|"
    r"del|erase|rd|rmdir|ri|rm"  # rm = Remove-Item-Alias
    r")\b)"
    # Methodenaufrufe umgehen die Cmdlet-Erkennung -> explizit destruktiv werten.
    # Das '\s*\(' verlangt die Klammer direkt hinter dem Namen; lesende Methoden
    # wie .StartsWith(...) bleiben unberuehrt.
    r"|(?:\.(?:Delete|Kill|Stop|Remove|Move|Terminate|Dispose|"
    r"Create|Invoke|Start|Put|SetInfo|Change|Rename|SetValue|SetPassword)\s*\()",
    re.IGNORECASE,
)

_WRITE_PATTERNS = re.compile(
    r"\b("
    r"Set-|New-|Start-|Stop-|Restart-|Enable-|Disable-|"
    r"Install-|Update-|Add-|Move-|Rename-|Copy-Item|"
    r"Out-File|Set-Content|Add-Content|"
    r"Register-|Unregister-|Grant-|Revoke-|"
    r"mkdir|md|icacls|takeown|net\s+(start|stop|user)"
    r")\b",
    re.IGNORECASE,
)

_SUSPICIOUS_DYNAMIC_PATTERNS = re.compile(
    r"("
    r"&\s*\([^)]*\+[^)]*\)|"
    #--- Aufruf-Operator auf eine VARIABLE ('& $c', '. ${c}'): was der Name
    #--- enthaelt, steht erst zur Laufzeit fest -- das Cmdlet bekommt der
    #--- Klassifizierer nie zu sehen, weil es als String-Literal weggeschnitten
    #--- wird. Nur an Kommandoposition, sonst traefe es auch '$obj.$prop'.
    r"(?m:^|[|;=({&])\s*[&.]\s*\$|"
    r"\b(Invoke-Expression|iex)\b|"
    r"\[System\.Reflection\.|"
    r"::LoadWithPartialName|"
    r"\bAdd-Type\b|"
    r"\bNew-Object\b|"
    r"\(Get-Command\b[^)]*\)\.Invoke\s*\("
    r")",
    re.IGNORECASE,
)

#--- Das \s* steht NACH der Anker-Gruppe, gilt also auch fuer '^'. Sonst liefert
#--- eine EINGERUECKTE Zeile keinen Kommando-Kopf und faellt aus der Pruefung.
_COMMAND_HEAD_RE = re.compile(
    r"(?im)(?:^|[|;&=])\s*([A-Za-z][\w-]*|%|\?|dir|ls|gci|gi|gc|cat|type|pwd)\b"
)

_READ_COMMANDS = {
    "cat",
    "convertfrom-json",
    "convertto-csv",
    "convertto-json",
    "dir",
    "format-list",
    "format-table",
    "format-wide",
    "gc",
    "gci",
    "get-acl",
    "get-childitem",
    "get-command",
    "get-content",
    "get-date",
    "get-eventlog",
    "get-hotfix",
    "get-item",
    "get-itemproperty",
    "get-localgroup",
    "get-localuser",
    "get-netadapter",
    "get-netfirewallrule",
    "get-netipaddress",
    "get-netroute",
    "get-process",
    "get-service",
    "get-volume",
    "gi",
    "group-object",
    "ls",
    "measure-object",
    "out-string",
    "pwd",
    "resolve-path",
    "select-object",
    "sort-object",
    "test-connection",
    "test-netconnection",
    "test-path",
    "type",
    "where-object",
    "write-host",
    "write-output",
    "?",
    "%",
}

#--- Native Ops-Kommandos mit NUR-LESENDEN Unterbefehlen. 'docker' ist kein
#--- Cmdlet und fiel sonst auf den WRITE-Default.
#--- Unterbefehl-genau: 'docker run/rm/stop/exec' bleibt WRITE bzw. DESTRUCTIVE.
#--- BEWUSST NICHT drin: 'docker inspect' und 'docker logs' -- sie geben die
#--- Container-Umgebung aus und waeren ein Secret-Leseweg an _BLOCKED_PATTERNS
#--- vorbei. Moeglich bleiben sie, aber mit Bestaetigung.
_NATIVE_READ_SUBCOMMANDS = {
    "docker": {"ps", "images", "image", "version", "info",
               "stats", "port", "top", "diff", "history", "events"},
    "kubectl": {"get", "top", "version", "api-resources"},
    "git":     {"status", "log", "diff", "show", "branch", "remote"},
}

_NATIVE_READ_RE = re.compile(
    r"(?im)(?:^|[|;&]\s*)(" + "|".join(_NATIVE_READ_SUBCOMMANDS) + r")(?:\.exe)?\s+([a-z][\w-]*)"
)


def _native_calls_are_read_only(text: str) -> bool | None:
    '''True  = alle Aufrufe dieser Tools sind lesende Unterbefehle
       False = mindestens einer ist es nicht
       None  = keines dieser Tools kommt vor (Aufrufer entscheidet wie bisher)'''
    found = False
    for m in _NATIVE_READ_RE.finditer(text):
        found = True
        tool, sub = m.group(1).lower(), m.group(2).lower()
        if sub not in _NATIVE_READ_SUBCOMMANDS.get(tool, ()):
            return False
    return True if found else None


#--- Die geschweifte Form ${...} MUSS mit: sie ist derselbe Variablenname, fiel
#--- aber aus dem Muster und damit aus der Regel "Zuweisung -> nie READ". Ein
#--- '${c} = "Remove-Item"' plus '& ${c} ...' lief so als READ ohne Bestaetigung.
_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*\$(?:\{[^}]*\}|[A-Za-z_][\w:.-]*)\s*=",
    re.IGNORECASE,
)

# Output-Redirection (>, >>, 2>, *>) schreibt Dateien, ist aber kein Cmdlet.
# Geprueft auf dem String-bereinigten Text, damit ein '>' in einem Literal kein
# False-Positive ist.
_REDIRECT_RE = re.compile(r"\d*>>?|\*>")

# Verb-Prefixe, die per PowerShell-Konvention rein lesend sind. Destruktive
# Format-/Get-Cmdlets greifen ueber _BLOCKED_PATTERNS davor.
_READ_VERB_PREFIXES = (
    "get-", "test-", "measure-", "select-", "sort-", "where-",
    "group-", "compare-", "format-", "convertto-", "convertfrom-",
)

#--- Datei-INHALTE ausserhalb der Ops-Pfade: kein Block, sondern Herabstufung
#--- READ -> WRITE, also eine Bestaetigung. _BLOCKED_PATTERNS deckt nur Dateien
#--- ab, an die beim Schreiben jemand gedacht hat; ein Get-Content laeuft als
#--- Administrator und ist bei Prompt-Injection der Massen-Leseweg.
#--- Objekt-Lesendes (Get-Service, docker ps) bleibt unbestaetigt.
_CONTENT_READ_CMDLETS = re.compile(
    r"(?im)(?:^|[|;&=({])\s*"
    r"(?:get-content|gc|cat|type|select-string|sls|import-csv|import-clixml)\b"
)

#--- Pfad-artige Tokens, gequotet wie ungequotet. Laeuft auf dem Text MIT
#--- String-Literalen -- Pfade stehen meist in Quotes.
_PATHISH_RE = re.compile(r'''(?i)(?:[a-z]:[\\/]|\\\\|\.{1,2}[\\/])[^\s"';|)}]*''')

#--- Ohne Bestaetigung inhaltlich lesbar. Erweiterbar ueber die .env:
#--- POWERSHELL_READ_PATH_ALLOWLIST="C:\Argus_Workspace;D:\Projekte"
_READ_PATH_ALLOWLIST = tuple(
    p.strip().rstrip("\\/").lower().replace("/", "\\")
    for p in os.getenv(
        "POWERSHELL_READ_PATH_ALLOWLIST", "C:\\Argus_Workspace"
    ).split(";")
    if p.strip()
)


def _path_is_allowed(token: str) -> bool:
    norm = token.strip().strip("\"'").rstrip("\\/").lower().replace("/", "\\")
    if not norm:
        return False
    #--- '..' kann aus einem erlaubten Praefix herausfuehren und ist rein
    #--- textuell nicht aufloesbar.
    if ".." in norm:
        return False
    for allowed in _READ_PATH_ALLOWLIST:
        if norm == allowed or norm.startswith(allowed + "\\"):
            return True
    return False


def _reads_content_outside_allowlist(clean: str) -> bool:
    '''True, wenn das Skript Datei-INHALTE liest und dabei mindestens ein Pfad ausserhalb
    der Allowlist im Spiel ist. Fail-closed: findet sich zu einem inhaltslesenden Cmdlet
    ueberhaupt kein erkennbarer Pfad (Variable, Pipe-Eingang), gilt das ebenfalls als
    ausserhalb -- was der Klassifizierer nicht sieht, darf er nicht freigeben.'''
    if not _CONTENT_READ_CMDLETS.search(clean):
        return False
    tokens = _PATHISH_RE.findall(clean)
    if not tokens:
        return True
    return not all(_path_is_allowed(t) for t in tokens)


def _strip_line_comments(text):
    # Quote-bewusstes Entfernen von '#'-Zeilenkommentaren: ein '#' INNERHALB
    # eines String-Literals ist in PowerShell kein Kommentar. Naives re.sub
    # wuerde ein destruktives Cmdlet dahinter als READ tarnen.
    out = []
    in_single = in_double = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ("\r", "\n"):
            in_single = in_double = False
            out.append(c)
        elif c == "'" and not in_double:
            in_single = not in_single
            out.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            out.append(c)
        elif c == "#" and not in_single and not in_double:
            while i < n and text[i] not in ("\r", "\n"):
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


#--- Platzhalter fuer ESCAPTE Quotes: weder Quote noch Wortzeichen.
_ESCAPE_PLACEHOLDER = "\x00"


def _defuse_escapes(text: str) -> str:
    '''Loest PowerShell-Backtick-Escapes auf, BEVOR String-Literale entfernt werden.

    Vorher wurden Backticks pauschal geloescht (clean.replace("`", "")) -- und zwar VOR
    _strip_quoted_strings. Damit wurde aus einem escapten `" ein ECHTES " und die
    Quote-Paarung verschob sich. Ein Angreifer konnte so ein destruktives Cmdlet in ein
    scheinbares String-Literal legen, das der Klassifizierer wegschnitt, PowerShell aber
    ausfuehrte -- Ergebnis: READ, also weder Bestaetigung noch Native-Exec-Veto:

        Get-Date; Write-Output `"; Remove-Item C:\\Temp\\x; Write-Output `"

    Jetzt: escapte Quotes werden zum Platzhalter (nie Delimiter), alle anderen Escapes
    kollabieren auf ihr Zeichen -- damit bleibt die Tarnung Remo`ve-Item -> Remove-Item
    weiterhin erkannt.'''
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "`" and i + 1 < n:
            nxt = text[i + 1]
            out.append(_ESCAPE_PLACEHOLDER if nxt in ('"', "'") else nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_comments_and_herestrings(script: str) -> str:
    # Block-Kommentare und Here-Strings VOR der quote-bewussten Bereinigung
    # entfernen. Die Reihenfolge ist sicherheitskritisch.
    clean = re.sub(r"<#.*?#>", "", script, flags=re.DOTALL)
    clean = re.sub(r'@"\r?\n.*?\r?\n"@', "", clean, flags=re.DOTALL)
    clean = re.sub(r"@'\r?\n.*?\r?\n'@", "", clean, flags=re.DOTALL)
    return _strip_line_comments(clean)


def _strip_quoted_strings(text: str) -> str:
    # String-Literale entfernen gegen False Positives.
    # AUSNAHME doppelte Quotes MIT Subexpression: PowerShell FUEHRT "$(...)" aus.
    # Weggeschnitten waere  Get-Date; Write-Output "$(cmd /c del ...)"  ein READ,
    # waehrend der Host loescht. Einfache Quotes sind literal und duerfen weg.
    def _keep_subexpressions(m):
        body = m.group(0)
        return body if "$(" in body else ""

    text = re.sub(r'"[^"]*"', _keep_subexpressions, text, flags=re.DOTALL)
    return re.sub(r"'[^']*'", "", text, flags=re.DOTALL)


def _clean_script(script: str) -> str:
    '''Escapes entschaerfen, dann Kommentare/Here-Strings entfernen. String-Literale
    bleiben erhalten (der BLOCKED-Check muss auch in Strings greifen).'''
    return _strip_comments_and_herestrings(_defuse_escapes(script))


def _scan_keep_vars(clean: str) -> str:
    '''Vorstufe von _scan_script: String-Literale sind weg, Variablenreferenzen
    noch da. Der Aufruf UEBER eine Variable ('& $c') ist nur hier sichtbar --
    nach dem Variablen-Strip fehlt genau das '$', an dem er haengt.'''
    scan = _strip_quoted_strings(clean)
    #--- '$(' eroeffnet eine Subexpression -- ein EIGENER Befehl. Als Trennzeichen
    #--- normalisieren, sonst steht das Kommando dahinter an keiner erkennbaren
    #--- Kopfposition. Muss VOR dem Variablen-Strip stehen.
    return scan.replace("$(", ";")


def _scan_script(clean: str) -> str:
    '''String-Literale und Variablenreferenzen entfernen -- die Textform, auf der
    Klassifizierung, Native-Exec-Veto und Trigger-Anzeige EINHEITLICH arbeiten.'''
    #--- Beide Schreibweisen: '$c' und '${beliebiger name}'. Die geschweifte Form
    #--- blieb sonst als Text stehen und wurde je nach Inhalt mal als Kommando-
    #--- kopf, mal als destruktives Cmdlet gelesen -- ein Variablenname ist beides
    #--- nicht. Den Aufruf ueber die Variable faengt _SUSPICIOUS_DYNAMIC_PATTERNS
    #--- auf der Vorstufe ab.
    return re.sub(r"\$(?:\{[^}]*\}|[a-zA-Z_][\w:]*)", "", _scan_keep_vars(clean))


def classify_script(script: str) -> ToolClassification:
    clean = _clean_script(script)

    # Wenn nach Entfernen von Kommentaren leer, ist es eine harmlose Leseoperation
    if not clean.strip():
        return ToolClassification.READ

    # Blockierte Muster auf dem Text MIT String-Literalen.
    if _BLOCKED_PATTERNS.search(clean):
        return ToolClassification.BLOCKED

    # Fuer die weitere Klassifizierung String-Literale entfernen. Wird ein
    # Here-String per iex ausgefuehrt, greifen _SUSPICIOUS_DYNAMIC_PATTERNS.
    clean_for_scan = _scan_script(clean)

    #--- Dynamik-Check auf der Vorstufe MIT Variablen: '& $c' verliert sein '$'
    #--- im Strip. Mehr Text heisst hier nur mehr Treffer -- fail-closed.
    if _SUSPICIOUS_DYNAMIC_PATTERNS.search(_scan_keep_vars(clean)):
        return ToolClassification.DESTRUCTIVE
    if _DESTRUCTIVE_PATTERNS.search(clean_for_scan):
        return ToolClassification.DESTRUCTIVE
    if _WRITE_PATTERNS.search(clean_for_scan):
        return ToolClassification.WRITE
    # Redirection schreibt Dateien -> mindestens WRITE (nie READ).
    if _REDIRECT_RE.search(clean_for_scan):
        return ToolClassification.WRITE

    # Zuweisung -> nie READ: ein Skript, das Zustand aufbaut, ist kein reiner
    # Lesevorgang (fail-closed wie der Rest des Gates).
    if _ASSIGNMENT_RE.search(clean):
        return ToolClassification.WRITE

    # Native Ops-Tools mit lesendem Unterbefehl als READ durchlassen, aber nur
    # wenn nichts anderes im Skript dagegen spricht.
    native_read = _native_calls_are_read_only(clean_for_scan)
    if native_read is False:
        return ToolClassification.WRITE

    # Datei-Inhalte ausserhalb der Ops-Pfade -> Bestaetigung. Auf clean geprueft.
    if _reads_content_outside_allowlist(clean):
        return ToolClassification.WRITE

    # Allow-List-Gate (fail-closed): READ nur wenn JEDER Pipeline-Kopf eindeutig
    # lesend ist. Alles andere faellt in den WRITE-Default.
    commands = [m.group(1).lower() for m in _COMMAND_HEAD_RE.finditer(clean_for_scan)]
    known_read = set(_NATIVE_READ_SUBCOMMANDS)
    if commands and all(
        cmd in _READ_COMMANDS
        or cmd.startswith(_READ_VERB_PREFIXES)
        or (cmd in known_read and native_read)
        for cmd in commands
    ):
        return ToolClassification.READ

    return ToolClassification.WRITE



class RunPowerShellInput(BaseModel):
    script: str = Field(
        description="PowerShell script to run on the host. "
                    "Read commands (Get-*, Test-*) run immediately. "
                    "Changes require confirmation, deletions double confirmation.",
    )
    timeout: int = Field(
        default=30,
        description="Timeout in seconds (max 300).",
        ge=5,
        le=300,
    )


#--- Native-Programm-Block: native Binaries koennen eine frische FullLanguage-
#    Shell starten, Code nachladen oder an der Cmdlet-Klassifizierung vorbei
#    Dienste/Registry/Netz aendern. Enthaelt Lader, Script-Hosts, Proxy-Exec,
#    Persistenz und System-Binaries -- pwsh eingeschlossen.
#
#    Verankert auf KOMMANDO-POSITION (Zeilenanfang oder nach |;&=({). Die
#    Anker-Klasse enthaelt bewusst '{' und '(': ein Skriptblock ist eine
#    Kommandoposition. '\s*' hinter der Anker-Gruppe, damit auch eingerueckte
#    Zeilen greifen.
#
#    Zweite Alternative: Aufruf ueber den PFAD (.\x.exe). Nur an Kommando-
#    position -- als ARGUMENT bleibt der Pfad erlaubt.
_NATIVE_EXEC_PATTERNS = re.compile(
    r"(?im)(?:^|[|;&=({])\s*(?:"
    r"(?:"
    r"sc|netsh|reg|cmd|powershell|pwsh|vssadmin|wmic|certutil|bitsadmin|curl|"
    r"mshta|rundll32|regsvr32|schtasks|at|wscript|cscript|msiexec|dism|bcdedit|"
    r"wsl|nltest|psexec|installutil|regasm|regsvcs|odbcconf"
    r")(?:\.exe)?\b"
    r"|"
    r"[&.]?\s*[\w:\\/.-]*\.(?:exe|bat|cmd|ps1|vbs|jse?|wsf|msi|scr|com|pif)\b"
    r")",
)


class RunPowerShellTool(BaseTool):
    name = "run_powershell"
    description = (
        "Runs an arbitrary PowerShell script on the Windows host. "
        "Read commands (Get-*, Test-*, dir) run immediately. "
        "Writing commands (Set-*, New-*, Copy-*) require confirmation. "
        "Deleting commands (Remove-*, del) require double confirmation. "
        "Reading FILE CONTENT (Get-Content, Select-String, Import-Csv) outside the "
        "workspace also requires confirmation -- listing files and reading system "
        "state stay immediate. "
        "Certain dangerous commands (Format-Volume, Stop-Computer) are always blocked. "
        "Calling native binaries (cmd, powershell, pwsh, sc, reg, or any .exe/.bat "
        "path) is rejected -- use PowerShell cmdlets."
    )
    classification = ToolClassification.WRITE
    timeout_seconds = 30

    def get_input_schema(self) -> type[BaseModel]:
        return RunPowerShellInput

    #--- Der Audit-/Confirmation-Flow lebt komplett in BaseTool.run(). Lokale
    #    Klassifizierung pro Aufruf, KEINE Mutation von self.classification --
    #    die Tool-Instanz ist ein Singleton.

    def _classify(self, params: RunPowerShellInput) -> ToolClassification:
        return classify_script(params.script)

    def _audit_details(self, params: RunPowerShellInput) -> str:
        return params.script

    def _veto(self, params: RunPowerShellInput, detected: ToolClassification):
        if detected == ToolClassification.BLOCKED:
            return (
                "blocked",
                "This script contains blocked commands and will not be executed.",
            )
        #--- Native-Exec-Veto gilt fuer JEDE Klassifizierung, auch READ. Ein
        #    natives Binary ist nie ein harmloser Lesevorgang.
        if _NATIVE_EXEC_PATTERNS.search(_scan_script(_clean_script(params.script))):
            return (
                "native_exec_blocked",
                "Execution rejected: the script invokes native Windows binaries "
                "(such as cmd, powershell, pwsh, sc, netsh, reg, schtasks, certutil) "
                "that can escape Constrained Language Mode and bypass the cmdlet-based "
                "classification. Use PowerShell cmdlets instead.",
            )
        #--- Kein Dry-Run auf dem echten Host. $WhatIfPreference ist KEINE
        #    Sicherheitsgrenze -- Befehle ohne ShouldProcess ignorieren -WhatIf
        #    und haetten ihren Seiteneffekt vor der Bestaetigung.
        return None

    def _impact_description(self, params: RunPowerShellInput) -> str:
        # classify_script ist eine reine Funktion -- der erneute Aufruf haelt die
        # Hook-Signatur einheitlich.
        return self._build_impact_description(params, classify_script(params.script))

    async def _execute(self, params: RunPowerShellInput) -> ToolOutput:
        result = await run_powershell(params.script, timeout=params.timeout, constrained=True)
        # Ausgabe deckeln: ein READ laeuft als Admin und waere sonst bei einer
        # Prompt-Injection ein Massen-Exfil-Kanal.
        _max = int(os.getenv("POWERSHELL_MAX_OUTPUT_CHARS", "20000"))

        def _cap(s: str) -> str:
            if s and len(s) > _max:
                return s[:_max] + f"\n... [output truncated after {_max} chars]"
            return s

        if result["had_errors"]:
            if result["stdout"].strip():
                return ToolOutput(
                    success=False,
                    data=_cap(result["stdout"]),
                    error=result["stderr"],
                )
            return ToolOutput(success=False, error=result["stderr"])
        return ToolOutput(success=True, data=_cap(result["stdout"]) or "Script succeeded (no output).")

    def _build_impact_description(self, params: RunPowerShellInput, detected: ToolClassification, dry_run_output: str | None = None) -> str:
        # Skript-Preview bis 1500 Zeichen -- Telegram erlaubt 4096, der Rest der
        # Nachricht belegt ~200.
        PREVIEW_LIMIT = 1500
        script = params.script
        preview = script[:PREVIEW_LIMIT]
        if len(script) > PREVIEW_LIMIT:
            preview += f"\n... [{len(script) - PREVIEW_LIMIT} weitere Zeichen gekürzt]"

        # Trigger-Tokens anhaengen, damit bei langen Skripten klar ist, welcher
        # Befehl die Klassifizierung ausgeloest hat.
        triggers: list[str] = []
        clean_no_str = _scan_script(_clean_script(script))
        for pattern_re, label in [
            (_DESTRUCTIVE_PATTERNS, "DESTRUCTIVE"),
            (_BLOCKED_PATTERNS, "BLOCKED"),
            (_SUSPICIOUS_DYNAMIC_PATTERNS, "DYNAMIC-EXEC"),
        ]:
            for m in pattern_re.finditer(clean_no_str):
                token = m.group(0).strip()
                if token:
                    triggers.append(f"{label}:{token[:40]}")
        trigger_info = ""
        if triggers:
            trigger_info = "\nErkannte Trigger: " + ", ".join(dict.fromkeys(triggers))

        level = detected.value.upper()
        desc = f"[{level}] PowerShell ausführen:{trigger_info}\n\n{preview}"
        if dry_run_output:
            desc += f"\n\n--- DRY-RUN OUTPUT ---\n{dry_run_output[:800]}"
            if len(dry_run_output) > 800:
                desc += "\n... [gekürzt]"
        return desc
"""

#--- Telegram-Bot

TELEGRAM_BOT_PY = r"""
import asyncio
import base64
import io
import logging
import os
import re
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_application = None
_http_client: Optional[httpx.AsyncClient] = None


#--- Telegram-Credentials kommen via load_secret aus /run/secrets/telegram.

def _bot_token() -> str:
    from rag_backend.crypto_utils import load_secret
    return load_secret("TELEGRAM_BOT_TOKEN")


def _allowed_chat_id() -> int:
    from rag_backend.crypto_utils import load_secret
    try:
        return int(load_secret("TELEGRAM_CHAT_ID") or "0")
    except (TypeError, ValueError):
        return 0


#--- Pro-Session Chat-History, bewusst in-memory und nicht Postgres-synchron.
_chat_histories: dict[int, deque] = {}
_HISTORY_LIMIT = int(os.getenv("TELEGRAM_HISTORY_LIMIT", "12"))

TTS_SERVICE_URL = os.getenv("TTS_SERVICE_URL", "http://tts-service:8002")
STT_SERVICE_URL = os.getenv("STT_SERVICE_URL", "http://stt-service:8003")


def _get_http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=60.0)
    return _http_client


def _get_history(chat_id: int) -> list:
    if chat_id not in _chat_histories:
        _chat_histories[chat_id] = deque(maxlen=_HISTORY_LIMIT * 2)
    hist = _chat_histories[chat_id]
    from langchain_core.messages import HumanMessage, AIMessage
    messages = []
    for role, text in hist:
        if role == "human":
            messages.append(HumanMessage(content=text))
        else:
            messages.append(AIMessage(content=text))
    return messages


def _add_to_history(chat_id: int, role: str, text: str):
    if chat_id not in _chat_histories:
        _chat_histories[chat_id] = deque(maxlen=_HISTORY_LIMIT * 2)
    _chat_histories[chat_id].append((role, text))


def _clear_history(chat_id: int):
    if chat_id in _chat_histories:
        _chat_histories[chat_id].clear()


#--- Voice-Settings (persistiert in telegram_chat_settings)

def _read_voice_enabled(chat_id: int) -> bool:
    try:
        from rag_backend.database import SessionLocal
        from rag_backend.models import TelegramChatSettings
        with SessionLocal() as db:
            row = db.get(TelegramChatSettings, chat_id)
            return bool(row.voice_enabled) if row else False
    except Exception as e:
        log.warning(f"voice_enabled read failed: {e}")
        return False


def _write_voice_enabled(chat_id: int, enabled: bool) -> None:
    try:
        from datetime import datetime, timezone
        from rag_backend.database import SessionLocal
        from rag_backend.models import TelegramChatSettings
        with SessionLocal() as db:
            row = db.get(TelegramChatSettings, chat_id)
            if row is None:
                row = TelegramChatSettings(chat_id=chat_id, voice_enabled=enabled,
                                           updated_at=datetime.now(timezone.utc))
                db.add(row)
            else:
                row.voice_enabled = enabled
                row.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as e:
        log.warning(f"voice_enabled write failed: {e}")


#--- TTS/STT-Calls

#--- Sprachnachrichten-Limit: Bereinigung und Kuerzung passieren im tts-service.
_TTS_MAX_CHARS = 800


async def _tts_speak(text: str) -> Optional[bytes]:
    if not text or not text.strip():
        return None
    try:
        resp = await _get_http().post(
            f"{TTS_SERVICE_URL}/v1/audio/speech",
            json={"input": text, "response_format": "ogg", "max_chars": _TTS_MAX_CHARS},
            timeout=300.0,
        )
        if resp.status_code != 200:
            log.warning(f"TTS HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.content
    except Exception as e:
        log.warning(f"TTS-Call fehlgeschlagen ({type(e).__name__}): {e}", exc_info=True)
        return None


async def _stt_transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> Optional[str]:
    if not audio_bytes:
        return None
    try:
        files = {"file": (filename, audio_bytes, "audio/ogg")}
        data = {"language": os.getenv("WHISPER_LANGUAGE", "de")}
        resp = await _get_http().post(
            f"{STT_SERVICE_URL}/v1/audio/transcriptions",
            files=files, data=data, timeout=120.0,
        )
        if resp.status_code != 200:
            log.warning(f"STT HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        payload = resp.json()
        return (payload.get("text") or "").strip()
    except Exception as e:
        log.warning(f"STT-Call fehlgeschlagen: {e}")
        return None


def _html_split(text: str, max_len: int = 4000) -> list[str]:
    # Splittet an Zeilengrenzen, um HTML-Tags nicht zu brechen.
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # Einzelzeile laenger als Limit: hart splitten
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    if not chunks:
        return [""]
    # Ein mehrzeiliges <blockquote expandable> darf nicht ueber die Chunk-Grenze
    # brechen: Telegram lehnt unbalanciertes HTML mit 400 ab. Offenes Tag am
    # Chunk-Ende schliessen, im Folge-Chunk wieder oeffnen.
    balanced: list[str] = []
    reopen = False
    for ch in chunks:
        if reopen:
            ch = "<blockquote expandable>" + ch
        reopen = ch.count("<blockquote") > ch.count("</blockquote>")
        if reopen:
            ch = ch + "</blockquote>"
        balanced.append(ch)
    return balanced


async def _send_chunked(bot, chat_id: int, text: str, reply_to: int | None = None, parse_mode: str | None = "HTML"):
    chunks = _html_split(text)
    sent_msg = None
    for i, chunk in enumerate(chunks):
        kwargs = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
        if i == 0 and reply_to:
            kwargs["reply_to_message_id"] = reply_to
        try:
            sent_msg = await bot.send_message(**kwargs)
        except Exception as e:
            # HTML-Parse-Fehler -> Fallback auf Plain (Telegram lehnt sonst die Nachricht ab)
            log.warning(f"send_message HTML fehlgeschlagen, Plain-Fallback: {e}")
            try:
                sent_msg = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=None)
            except Exception as e2:
                log.error(f"send_message Plain-Fallback ebenfalls fehlgeschlagen: {e2}")
    return sent_msg


# Markdown-Links [Text](url) mit https?://-Pflicht -- das verhindert
# javascript:/data:-Scheme-Injection.
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')


def _md_to_plain(text: str) -> str:
    '''Reduziert Markdown auf reinen Fliesstext fuer die Telegram-Anzeige (mobil genutzt).
    Telegram rendert kein Markdown, und in Telegram sind KEINE Tabellen/Listen/Fett-Marker
    gewuenscht -- die volle Struktur gibt es nur im WebUI. Marker werden daher entfernt statt
    umgewandelt; Tabellen werden in lesbare Zeilen ("Zelle - Zelle") aufgeloest. Eingabe ist
    bereits html-escaped; <a>/<blockquote> aus den vorherigen Schritten bleiben erhalten.'''
    # Codebloecke: Fences weg, Inhalt als Text behalten.
    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    # Reste von Markdown-Links ohne http (echte wurden schon zu <a>) -> nur Linktext behalten.
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)\s]*\)", r"\1", text)
    # Markdown-Tabellen mobil lesbar machen.
    text = re.sub(r"(?m)^[ \t]*\|?[ \t:|-]*-[ \t:|-]*\|?[ \t]*$\n?", "", text)
    text = re.sub(
        r"(?m)^[ \t]*\|(.+)\|[ \t]*$",
        lambda m: " – ".join(c.strip() for c in m.group(1).split("|") if c.strip()),
        text,
    )
    # ATX-Header -> nur der Text (keine Raute).
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", r"\1", text)
    # Listen-Bullets am Zeilenanfang -> Marker weg.
    text = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "", text)
    # Inline-Emphase: nur die Marker entfernen, Text bleibt.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Aufraeumen: Trailing-Spaces und 3+ Leerzeilen.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_answer_html(answer: str) -> str:
    '''Escape gesamten Text, dann <think>-Block -> <blockquote expandable>,
    dann Markdown-Links [Text](url) -> klickbare <a>-Tags, danach wird die restliche
    Markdown-Formatierung auf reinen Fliesstext reduziert (Telegram = mobil, kein Markup;
    Tabellen/Listen/Fett gibt es nur im WebUI).
    Reihenfolge: erst html.escape (verhindert Telegram-400 bei Code/<Generics>),
    dann Think-Marker zurueckuebersetzen, dann Links injizieren, dann auf Fliesstext glaetten.
    Globales Escape bleibt erhalten (kein <a href> direkt aus Modell noetig).'''
    import html as _html
    escaped = _html.escape(answer)
    think_re = re.compile(r"&lt;think&gt;(.*?)&lt;/think&gt;", re.DOTALL)

    def _repl(m):
        inner = m.group(1).strip()
        if not inner:
            return ""
        return f"<blockquote expandable><b>\U0001f914 Gedankengang:</b>\n{inner}</blockquote>\n\n"

    cleaned = think_re.sub(_repl, escaped).strip()
    cleaned = cleaned if cleaned else escaped
    # Markdown-Links -> <a href>. Beides ist bereits entity-sicher.
    cleaned = _MD_LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', cleaned)
    # Restliche Markdown-Marker raus -> reiner Fliesstext (Telegram zeigt sonst rohe ** # | `).
    cleaned = _md_to_plain(cleaned)
    return cleaned


async def _send_voice_response(bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    # Bereinigung und Kuerzung macht der tts-service zentral.
    if not text or not text.strip():
        return
    audio = await _tts_speak(text)
    if not audio:
        return
    try:
        await bot.send_voice(chat_id=chat_id, voice=io.BytesIO(audio),
                             reply_to_message_id=reply_to)
    except Exception as e:
        log.warning(f"send_voice fehlgeschlagen: {e}")


async def _voice_command_handler(update, context):
    allowed = _allowed_chat_id()
    if update.message.chat_id != allowed:
        await update.message.reply_text("Nicht autorisiert.")
        return
    chat_id = update.message.chat_id
    arg = " ".join(context.args or []).strip().lower()
    if arg in ("on", "an", "ein"):
        _write_voice_enabled(chat_id, True)
        await update.message.reply_text("Voice-Antworten aktiviert. (/voice off zum Deaktivieren)")
    elif arg in ("off", "aus"):
        _write_voice_enabled(chat_id, False)
        await update.message.reply_text("Voice-Antworten deaktiviert.")
    else:
        current = _read_voice_enabled(chat_id)
        state = "AN" if current else "AUS"
        await update.message.reply_text(
            f"Voice-Antworten: {state}\nNutzung: /voice on  oder  /voice off"
        )


async def _clear_command_handler(update, context):
    allowed = _allowed_chat_id()
    if update.message.chat_id != allowed:
        await update.message.reply_text("Nicht autorisiert.")
        return
    _clear_history(update.message.chat_id)
    await update.message.reply_text("🧹 Chat-Verlauf wurde gelöscht. Neuer Kontext gestartet!")


async def _run_agent(chat_id: int, user_text: str, context, reply_to: int, force_voice: bool = False,
                     images: list | None = None):
    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(stop_typing.wait()), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    typing_task = asyncio.create_task(_keep_typing())
    try:
        from rag_backend.agent import agent_executor
        history = _get_history(chat_id)
        result = await agent_executor.ainvoke({
            "input": user_text,
            "chat_history": history,
            "user_id": int(os.getenv("TELEGRAM_AGENT_USER_ID", "0")),
            "images": images or [],
            "channel": "telegram",   # Missions von hier melden ihr Ergebnis per Telegram
        })
        answer = result.get("output", "Keine Antwort.")
    except Exception as e:
        log.error(f"agent_executor Fehler: {e}")
        answer = f"Fehler beim Ausfuehren: {e}"
    finally:
        stop_typing.set()
        typing_task.cancel()

    # History ist text-only -- Bilder nur als Marker.
    _add_to_history(chat_id, "human", ("[Bild] " if images else "") + user_text)
    # History speichert die rohe Modell-Antwort (Think-Tags entfernt), nicht das HTML
    from rag_backend.utils import strip_think
    raw_clean = strip_think(answer) or answer
    _add_to_history(chat_id, "ai", raw_clean)

    # Telegram-Versand als HTML mit collapsiblem Denkblock
    html_answer = _format_answer_html(answer)
    if not html_answer.strip():
        # Leere Modell-Antwort: send_message("") wuerde mit 400 abgelehnt.
        html_answer = "(Keine Antwort erhalten.)"
    await _send_chunked(context.bot, chat_id, html_answer, reply_to=reply_to, parse_mode="HTML")

    if _read_voice_enabled(chat_id) or force_voice:
        # Sprachnachricht im Hintergrund generieren, um den Hauptthread nicht zu blockieren
        async def _voice_task():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
            except Exception:
                pass
            try:
                await _send_voice_response(context.bot, chat_id, raw_clean, reply_to=reply_to)
            except Exception as ve:
                log.warning(f"Hintergrund-TTS fehlgeschlagen: {ve}")

        asyncio.create_task(_voice_task())


async def _text_handler(update, context):
    allowed = _allowed_chat_id()
    if update.message.chat_id != allowed:
        await update.message.reply_text("Nicht autorisiert.")
        return
    
    # Dynamische Erkennung fuer Sprachausgabe per Texteingabe
    user_text = update.message.text or ""
    # Wortgrenzen statt Substring: 'voice' in 'Invoice' loeste sonst aus.
    force_voice = bool(re.search(
        r"\b(?:sprich|lies vor|sprachnachricht|sprachausgabe|voice|tts|audio|sprach\s*nachricht)\b",
        user_text, re.IGNORECASE,
    ))

    await _run_agent(
        chat_id=update.message.chat_id,
        user_text=user_text,
        context=context,
        reply_to=update.message.message_id,
        force_voice=force_voice,
    )


async def _voice_message_handler(update, context):
    allowed = _allowed_chat_id()
    if update.message.chat_id != allowed:
        await update.message.reply_text("Nicht autorisiert.")
        return
    chat_id = update.message.chat_id
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        audio_bytes = buf.getvalue()
    except Exception as e:
        log.warning(f"Voice-Download fehlgeschlagen: {e}")
        await update.message.reply_text("Konnte Voice-Nachricht nicht laden.")
        return

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    transcript = await _stt_transcribe(audio_bytes, filename=f"voice_{voice.file_id}.ogg")
    if not transcript:
        await update.message.reply_text("Transkription fehlgeschlagen oder leer.")
        return

    await update.message.reply_text(f"\U0001f3a4 Verstanden: {transcript}")

    # Wenn der User per Sprache redet, antworten wir ebenfalls per Sprache
    await _run_agent(
        chat_id=chat_id,
        user_text=transcript,
        context=context,
        reply_to=update.message.message_id,
        force_voice=True,
    )


async def _photo_handler(update, context):
    # Foto oder als Datei gesendetes Bild -> base64-data-URI -> Agent.
    allowed = _allowed_chat_id()
    if update.message.chat_id != allowed:
        await update.message.reply_text("Nicht autorisiert.")
        return
    chat_id = update.message.chat_id

    file_id, mime = None, "image/jpeg"
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        doc = update.message.document
        if doc is not None and (doc.mime_type or "").startswith("image/"):
            # 7 MB roh: die Base64-URI waechst um ein Drittel.
            if doc.file_size and doc.file_size > 7 * 1024 * 1024:
                await update.message.reply_text("Bild zu gross (max. 7 MB).")
                return
            file_id, mime = doc.file_id, doc.mime_type
    if not file_id:
        return

    try:
        tg_file = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        img_bytes = buf.getvalue()
    except Exception as e:
        log.warning(f"Bild-Download fehlgeschlagen: {e}")
        await update.message.reply_text("Konnte das Bild nicht laden.")
        return

    data_uri = f"data:{mime};base64," + base64.b64encode(img_bytes).decode("ascii")
    # Explizit ablehnen statt spaeter still zu verwerfen.
    _max_uri = int(os.getenv("MAX_IMAGE_DATA_CHARS", 10 * 1024 * 1024))
    if len(data_uri) > _max_uri:
        await update.message.reply_text("Bild zu gross fuer die Verarbeitung.")
        return
    caption = (update.message.caption or "").strip() or "Beschreibe, was auf dem Bild zu sehen ist."

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    await _run_agent(
        chat_id=chat_id,
        user_text=caption,
        context=context,
        reply_to=update.message.message_id,
        images=[data_uri],
    )


async def _get_application():
    global _application
    if _application is not None:
        return _application
    token = _bot_token()
    if not token:
        return None
    try:
        from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        _application = Application.builder().token(token).build()

        async def _callback_handler(update, context):
            query = update.callback_query
            allowed = _allowed_chat_id()
            chat = update.effective_chat
            # Gegen die Chat-ID pruefen, nicht from_user.id -- gilt auch in Gruppen.
            if chat is None or chat.id != allowed:
                await query.answer("Nicht autorisiert.")
                return
            await query.answer()
            parts = (query.data or "").split(":", 2)
            if len(parts) != 3:
                return

            prefix, action, payload = parts[0], parts[1], parts[2]

            #--- Confirmation-Handler
            if prefix != "confirm":
                return
            conf_id = payload
            from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
            if action == "approve":
                req = confirmation_store.approve(conf_id, via="telegram")
                if req and req.status == ConfirmationStatus.COOLDOWN:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Endgültig bestätigen", callback_data=f"confirm:cooldown:{conf_id}"),
                        InlineKeyboardButton("❌ Abbrechen", callback_data=f"confirm:reject:{conf_id}"),
                    ]])
                    await query.edit_message_text("⚠️ DESTRUCTIVE — Cool-Down aktiv. Erneut bestätigen:", reply_markup=kb)
                    return
                status_text = req.status.value if req else "nicht gefunden"
                await query.edit_message_text(f"✅ Genehmigt ({status_text})")
            elif action == "cooldown":
                req, remaining = confirmation_store.confirm_after_cooldown(conf_id, via="telegram")
                if not req:
                    if remaining > 0:
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Endgültig bestätigen", callback_data=f"confirm:cooldown:{conf_id}"),
                            InlineKeyboardButton("❌ Abbrechen", callback_data=f"confirm:reject:{conf_id}"),
                        ]])
                        await query.edit_message_text(
                            f"⚠️ DESTRUCTIVE — noch {remaining:.1f}s Cool-Down, dann erneut bestätigen.",
                            reply_markup=kb,
                        )
                    else:
                        await query.edit_message_text("⏳ Anfrage abgelaufen oder nicht gefunden.")
                    return
                await query.edit_message_text(f"✅ Endgültig genehmigt ({req.status.value})")
            elif action == "reject":
                req = confirmation_store.reject(conf_id, via="telegram")
                status_text = req.status.value if req else "nicht gefunden"
                await query.edit_message_text(f"❌ Abgelehnt ({status_text})")

        from telegram.ext import CommandHandler
        _application.add_handler(CallbackQueryHandler(_callback_handler))
        _application.add_handler(CommandHandler("voice", _voice_command_handler))
        _application.add_handler(CommandHandler(["clear", "reset", "start"], _clear_command_handler))
        _application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _voice_message_handler))
        _application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, _photo_handler))
        _application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text_handler))
        return _application
    except Exception as e:
        log.warning(f"Telegram-Bot Init fehlgeschlagen: {e}")
        return None


async def start_bot():
    app = await _get_application()
    if app is None:
        log.info("Telegram-Bot nicht konfiguriert -- uebersprungen.")
        return
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            # Nur Update-Typen abonnieren, fuer die es Handler gibt.
            allowed_updates=["callback_query", "message"],
        )
        log.info("Telegram-Bot gestartet (Polling).")
    except Exception as e:
        log.warning(f"Telegram-Bot Start fehlgeschlagen: {e}")


async def stop_bot():
    global _application, _http_client
    if _application is not None:
        try:
            if _application.updater and _application.updater.running:
                await _application.updater.stop()
            await _application.stop()
            await _application.shutdown()
            log.info("Telegram-Bot gestoppt.")
        except Exception as e:
            log.warning(f"Telegram-Bot Stop fehlgeschlagen: {e}")
        finally:
            _application = None
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception:
            pass
        _http_client = None


async def send_confirmation_request(
    confirmation_id: str,
    tool_name: str,
    tool_class: str,
    impact_description: str,
):
    app = await _get_application()
    if app is None:
        return None
    chat_id = _allowed_chat_id()
    if not chat_id:
        return None
    try:
        import html as _html
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        emoji = {"read": "📖", "write": "✏️", "destructive": "⚠️"}.get(tool_class, "🔧")
        # HTML statt Markdown: impact_description enthaelt staendig Sonderzeichen,
        # bei parse_mode=Markdown lehnt die API ab und der Button kommt nie an.
        header = (
            f"{emoji} <b>Bestätigung erforderlich</b>\n\n"
            f"Tool: <code>{_html.escape(tool_name)}</code>\n"
            f"Klasse: <code>{_html.escape(tool_class.upper())}</code>\n"
            f"ID: <code>{_html.escape(confirmation_id)}</code>\n\n"
        )
        # NACH dem Escapen kuerzen: html.escape vervielfacht die Laenge.
        safe_impact = _html.escape(impact_description)
        room = 4000 - len(header)
        if len(safe_impact) > room:
            cut = safe_impact[:room]
            # Nicht mitten in einer &...;-Entity schneiden.
            back = max(cut.rfind(";"), cut.rfind(" "))
            if back > 0:
                cut = cut[:back + 1]
            safe_impact = cut + "\n... [gekürzt]"
        text = header + safe_impact
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Genehmigen", callback_data=f"confirm:approve:{confirmation_id}"),
            InlineKeyboardButton("❌ Ablehnen", callback_data=f"confirm:reject:{confirmation_id}"),
        ]])
        msg = await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        return msg
    except Exception as e:
        log.warning(f"Telegram send fehlgeschlagen: {e}")
        return None


async def edit_message_to_timeout(chat_id: int, message_id: int, tool_name: str):
    app = await _get_application()
    if app is None:
        return
    try:
        import html as _html
        text = f"⏳ <b>Zeitüberschreitung</b>\n\nDie Bestätigungsanfrage für Tool <code>{_html.escape(tool_name)}</code> ist abgelaufen."
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")
    except Exception as e:
        log.warning(f"Konnte Telegram-Nachricht nicht auf Timeout editieren: {e}")
"""

#--- MCP-Loader

MCP_LOADER_PY = r"""
import json
import logging
import os
from pathlib import Path
from contextlib import AsyncExitStack
from typing import Any

log = logging.getLogger(__name__)

_mcp_client = None
_mcp_stack: AsyncExitStack | None = None
_mcp_tools: list = []

#--- Default lebt im Code statt als escaped JSON in der .env (Composes Parser
#    liest Backslash-Quotes anders als python-dotenv).
_DEFAULT_MCP_SERVERS = (
    '[{"name":"fs","command":"python","args":["-m","rag_backend.mcp_servers.filesystem"]}]'
)


def _is_allowed_stdio_command(command: str, args: list) -> bool:
    cmd_name = Path(command).name.lower()
    if cmd_name in {"python", "python3", "python.exe"}:
        return len(args) >= 2 and args[0] == "-m" and str(args[1]).startswith("rag_backend.mcp_servers.")
    if not os.path.isabs(command):
        return False
    resolved = Path(command).resolve(strict=False)
    allowed_roots = [
        Path("/app").resolve(strict=False),
        Path("/host/argus_workspace").resolve(strict=False),
    ]
    return any(resolved == root or root in resolved.parents for root in allowed_roots)


def _is_allowed_mcp_url(url: str) -> bool:
    # url-Transport-MCP-Server default-deny: nur Hosts aus MCP_ALLOWED_URLS.
    # Leer -> kein remote MCP. Der stdio-fs-Default ist nicht betroffen.
    allow = [u.strip() for u in os.getenv("MCP_ALLOWED_URLS", "").split(",") if u.strip()]
    if not allow:
        return False
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for a in allow:
        a_host = (urlparse(a).hostname or a).lower() if "://" in a else a.lower()
        if host == a_host:
            return True
    return False


async def startup_mcp() -> list:
    global _mcp_client, _mcp_stack, _mcp_tools
    raw = os.getenv("MCP_SERVERS", "").strip()
    if raw.lower() in {"none", "off", "[]"}:
        return []
    if not raw:
        raw = _DEFAULT_MCP_SERVERS
    try:
        servers_config: list[dict[str, Any]] = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"MCP_SERVERS JSON-Fehler: {e}")
        return []
    if not servers_config:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        log.warning("langchain-mcp-adapters nicht installiert -- MCP-Tools uebersprungen.")
        return []
    servers: dict[str, dict] = {}
    for entry in servers_config:
        name = entry.get("name")
        if not name:
            continue
        if "url" in entry:
            if not _is_allowed_mcp_url(entry["url"]):
                log.warning("MCP url-Transport nicht erlaubt (MCP_ALLOWED_URLS): %s", entry["url"])
                continue
            servers[name] = {"url": entry["url"], "transport": entry.get("transport", "sse")}
        elif "command" in entry:
            cmd_raw = entry["command"]
            if isinstance(cmd_raw, list):
                if not cmd_raw:
                    continue
                command, args = cmd_raw[0], list(cmd_raw[1:])
            else:
                command = cmd_raw
                args = list(entry.get("args", []))
            if not _is_allowed_stdio_command(str(command), args):
                log.warning("MCP-Server command nicht erlaubt: %s", command)
                continue
            servers[name] = {
                "command": command,
                "args": args,
                "transport": "stdio",
                "env": entry.get("env", {}),
            }
    if not servers:
        return []
    try:
        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        _mcp_client = client
        _mcp_stack = None
        _mcp_tools = list(tools)
        log.info(f"MCP gestartet: {len(_mcp_tools)} Tools von {list(servers.keys())}")
        return list(_mcp_tools)
    except Exception as e:
        log.warning(f"MCP-Client Start fehlgeschlagen: {e}")
        _mcp_client = None
        _mcp_stack = None
        return []


async def shutdown_mcp():
    global _mcp_client, _mcp_stack, _mcp_tools
    _mcp_client = None
    _mcp_stack = None
    _mcp_tools = []
"""

#--- tools/__init__.py — zentrale Registrierung

TOOLS_INIT_PY = r"""
from rag_backend.action_engine.tools.disk_space import CheckDiskSpaceTool
from rag_backend.action_engine.tools.windows_updates import GetWindowsUpdatesTool
from rag_backend.action_engine.tools.eventlog import ReadEventlogTool
from rag_backend.action_engine.tools.windows_update_install import InstallWindowsUpdatesTool
from rag_backend.action_engine.tools.restart_service import RestartServiceTool
from rag_backend.action_engine.tools.run_powershell_tool import RunPowerShellTool


def register_all_read_tools():
    from rag_backend.action_engine.registry import tool_registry
    tool_registry.register(CheckDiskSpaceTool().to_langchain_tool())
    tool_registry.register(GetWindowsUpdatesTool().to_langchain_tool())
    tool_registry.register(ReadEventlogTool().to_langchain_tool())


def register_all_write_tools():
    from rag_backend.action_engine.registry import tool_registry
    tool_registry.register(InstallWindowsUpdatesTool().to_langchain_tool())
    tool_registry.register(RestartServiceTool().to_langchain_tool())
    # run_powershell ist als WRITE registriert (dynamische Pro-Aufruf-Klassifizierung in _classify).
    tool_registry.register(RunPowerShellTool().to_langchain_tool())
"""


# ---------------------------------------------------------------------------
# MCP-Server: Filesystem mit Path-Whitelist auf C:\Argus_Workspace
# ---------------------------------------------------------------------------

MCP_FILESYSTEM_INIT_PY = r"""
"""

MCP_FILESYSTEM_PY = r'''
#!/usr/bin/env python3
"""Argus MCP-Filesystem-Server.

Path-Whitelist: C:\\Argus_Workspace (Container-Mount: /host/argus_workspace).
Tools: fs_read_file, fs_write_file, fs_list_directory, fs_mkdir.
fs_delete bewusst weggelassen -- DESTRUCTIVE-Operationen laufen ueber
run_powershell mit BaseTool-Confirmation-Flow.
"""

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP


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
            f"The fs_* tools only see the workspace -- for paths outside use "
            f"run_powershell (e.g. Get-ChildItem)."
        )
    #--- Relative Pfade als Workspace-Relativ interpretieren
    return WORKSPACE_CONTAINER / s.lstrip("/\\")


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
    return real


_READONLY_SUBDIRS = ("identity",)


def _enforce_writable(real: Path) -> Path:
    """Verbietet Schreibzugriff auf Prompt-/Identitaetsdateien (identity/*). Diese werden in
    den System-Prompt geladen (get_prompt_from_env); waeren sie agent-schreibbar, koennte ein
    per Prompt-Injection gesteuerter Agent seinen eigenen System-Prompt umschreiben (M1/ASI06).
    users/* bleibt bewusst schreibbar (Agent-gepflegtes Gedaechtnis/Reflexion)."""
    workspace_real = WORKSPACE_CONTAINER.resolve(strict=False)
    for sub in _READONLY_SUBDIRS:
        try:
            real.relative_to(workspace_real / sub)
        except ValueError:
            continue
        raise PermissionError(
            f"Write access denied: '{sub}/' is read-only (identity/prompt files); read access only."
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
        content = content[:_max] + f"\n... [file truncated after {_max} chars -- read on selectively or use run_powershell]"
    _audit("read", _to_windows(p))
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
    items = []
    if recursive:
        for f in sorted(p.rglob("*")):
            suffix = "\\" if f.is_dir() else ""
            items.append(f"{_to_windows(f)}{suffix}")
    else:
        for f in sorted(p.iterdir()):
            suffix = "\\" if f.is_dir() else ""
            items.append(f"{_to_windows(f)}{suffix}")
    _audit("list", _to_windows(p))
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
'''


# ---------------------------------------------------------------------------
# Offline-Tests: Confirmation-/Audit-Flow (BaseTool.run) + Skript-Klassifizierung
# ---------------------------------------------------------------------------

TEST_ACTION_ENGINE_PY = r"""
'''Offline-Tests fuer den sicherheitskritischsten Pfad der Action-Engine:
BaseTool.run() (Audit -> Veto -> Confirmation-Gate -> Ausfuehrung) und die
run_powershell-Skript-Klassifizierung. Kein SSH, kein Telegram, keine DB --
alle Seams (write_audit_log, load_secret, confirmation_store) gemonkeypatcht.
Muster wie tests/test_cloud_agents.py; pytest.ini setzt asyncio_mode=auto.
'''
import pytest
from pydantic import BaseModel

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput


class _NoInput(BaseModel):
    pass


class _DummyTool(BaseTool):
    name = "dummy_tool"
    description = "test tool"

    def __init__(self, classification=ToolClassification.READ, veto=None):
        self._cls = classification
        self._veto_result = veto
        self.executed = False

    def get_input_schema(self):
        return _NoInput

    def _classify(self, params):
        return self._cls

    def _veto(self, params, detected):
        return self._veto_result

    async def _execute(self, params):
        self.executed = True
        return ToolOutput(success=True, data="done")


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []
    import rag_backend.action_engine.audit as audit_mod
    monkeypatch.setattr(audit_mod, "write_audit_log", lambda **kw: calls.append(kw))
    return calls


def _patch_secrets(monkeypatch, telegram_configured: bool):
    import rag_backend.crypto_utils as crypto_mod

    def _fake_load_secret(name, *a, **kw):
        if name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            return "x" if telegram_configured else ""
        return "x"

    monkeypatch.setattr(crypto_mod, "load_secret", _fake_load_secret)


def _patch_confirmation(monkeypatch, resolved_status):
    # resolved_status: ConfirmationStatus | None (None = Timeout/kein Ergebnis)
    import rag_backend.action_engine.confirmation as conf_mod

    class _Req:
        id = "req-test-1"

    def _create(**kwargs):
        return _Req()

    async def _wait(_id):
        if resolved_status is None:
            return None

        class _Resolved:
            status = resolved_status

        return _Resolved()

    monkeypatch.setattr(conf_mod.confirmation_store, "create", _create)
    monkeypatch.setattr(conf_mod.confirmation_store, "wait_for_resolution", _wait)
    return conf_mod


# ---- Confirmation-Gate ------------------------------------------------------

async def test_read_runs_without_confirmation(monkeypatch, audit_calls):
    _patch_secrets(monkeypatch, telegram_configured=False)
    tool = _DummyTool(ToolClassification.READ)
    out = await tool.run({})
    assert out.success is True and tool.executed is True
    assert audit_calls and audit_calls[-1]["status"] == "ok"
    assert audit_calls[-1]["tool_class"] == "read"


async def test_write_blocked_without_channel(monkeypatch, audit_calls):
    # OHNE Telegram-Secrets darf ein WRITE NIE ausgefuehrt werden (fail-closed).
    _patch_secrets(monkeypatch, telegram_configured=False)
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert "no confirmation channel" in (out.error or "")
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


async def test_destructive_blocked_without_channel(monkeypatch, audit_calls):
    _patch_secrets(monkeypatch, telegram_configured=False)
    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


async def test_veto_blocks_before_confirmation(monkeypatch, audit_calls):
    # Veto greift VOR dem Confirmation-Gate -- auch mit konfiguriertem Kanal.
    _patch_secrets(monkeypatch, telegram_configured=True)
    tool = _DummyTool(ToolClassification.WRITE, veto=("blocked", "vetoed"))
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert out.error == "vetoed"
    assert audit_calls[-1]["status"] == "blocked"


async def test_confirmation_approved_executes(monkeypatch, audit_calls):
    _patch_secrets(monkeypatch, telegram_configured=True)
    conf_mod = _patch_confirmation(monkeypatch, None)
    _patch_confirmation(monkeypatch, conf_mod.ConfirmationStatus.APPROVED)
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({})
    assert out.success is True and tool.executed is True
    assert audit_calls[-1]["status"] == "ok"


async def test_confirmation_rejected_blocks(monkeypatch, audit_calls):
    _patch_secrets(monkeypatch, telegram_configured=True)
    conf_mod = _patch_confirmation(monkeypatch, None)
    _patch_confirmation(monkeypatch, conf_mod.ConfirmationStatus.REJECTED)
    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_rejected"


async def test_confirmation_timeout_blocks(monkeypatch, audit_calls):
    _patch_secrets(monkeypatch, telegram_configured=True)
    _patch_confirmation(monkeypatch, None)  # wait_for_resolution -> None = Timeout
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_timeout"


# ---- run_powershell-Klassifizierung ----------------------------------------

def test_classify_script_levels():
    from rag_backend.action_engine.tools.run_powershell_tool import classify_script

    assert classify_script("Get-Process | Select-Object -First 3") == ToolClassification.READ
    assert classify_script("Set-Content -Path C:\\x.txt -Value y") == ToolClassification.WRITE
    assert classify_script("Remove-Item C:\\tmp\\x") == ToolClassification.DESTRUCTIVE
    assert classify_script("Format-Volume -DriveLetter C") == ToolClassification.BLOCKED
    # Secrets-Lesezugriff ist kein harmloser READ
    assert classify_script("Get-Content C:\\Agentic_AI\\API_Tokens\\x.txt") == ToolClassification.BLOCKED
    # Unbekanntes Cmdlet faellt fail-closed auf WRITE (Allow-List-Gate)
    assert classify_script("Do-Something") == ToolClassification.WRITE
    # Output-Redirection schreibt Dateien -> nie READ
    assert classify_script("Get-Process > C:\\out.txt") == ToolClassification.WRITE
    # '#' in String ist KEIN Kommentar -- Regression fuer den Confirmation-Bypass
    assert classify_script('Write-Output "#"; Remove-Item C:\\x') == ToolClassification.DESTRUCTIVE
    # Dynamische Ausfuehrung ist DESTRUCTIVE
    assert classify_script("Invoke-Expression $cmd") == ToolClassification.DESTRUCTIVE
    # Zuweisung ist kein reiner Lesevorgang -> Bestaetigung
    assert classify_script("$x = Get-Date") == ToolClassification.WRITE


def test_escaped_quote_no_read_bypass():
    '''Regression: escapte Quotes duerfen die String-Erkennung nicht verschieben.

    Frueher wurden Backticks VOR dem String-Strip global geloescht -- aus `" wurde ein
    echtes ", die Quote-Paarung verschob sich und der destruktive Teil verschwand im
    vermeintlichen String-Literal. Ergebnis: READ, also weder Bestaetigung noch Veto,
    waehrend PowerShell das Remove-Item ausfuehrte.'''
    from rag_backend.action_engine.tools.run_powershell_tool import classify_script

    payload = 'Get-Date; Write-Output `"; Remove-Item C:\\Temp\\x; Write-Output `"'
    assert classify_script(payload) == ToolClassification.DESTRUCTIVE
    # Gleicher Trick mit einem nativen Programm
    native = 'Get-Date; Write-Output `"; cmd /c whoami; Write-Output `"'
    assert classify_script(native) != ToolClassification.READ
    # Backtick-Tarnung eines Cmdlets bleibt erkannt
    assert classify_script("Remo`ve-Item C:\\tmp\\x") == ToolClassification.DESTRUCTIVE


def test_variable_invocation_no_read_bypass():
    '''Regression: die geschweifte Variablenform umging das Bestaetigungs-Gate.

    _ASSIGNMENT_RE kannte nur '$name', nicht '${name}'. Damit fiel
    '${c} = "Remove-Item"' aus der Regel "Zuweisung -> nie READ", und zusammen mit
    einem harmlosen Kommandokopf (Get-Process) ergab das READ: sofortige Ausfuehrung
    als Administrator ohne Rueckfrage. Das Cmdlet selbst stand im String-Literal und
    war nach dem Strip unsichtbar -- auch fuer das Native-Exec-Veto.'''
    from rag_backend.action_engine.tools.run_powershell_tool import classify_script

    hidden = '${c} = "Remove-Item"\nGet-Process\n& ${c} C:\\Temp\\x -Recurse'
    assert classify_script(hidden) == ToolClassification.DESTRUCTIVE
    # Gleicher Weg mit einem nativen Programm im Literal
    native = '${c} = "cmd"\nGet-Process\n& ${c} /c whoami'
    assert classify_script(native) == ToolClassification.DESTRUCTIVE
    # Klassische Schreibweise: der Aufruf UEBER eine Variable zaehlt genauso
    assert classify_script('$c = "Copy-Item"\nGet-Date\n& $c C:\\a C:\\b') == ToolClassification.DESTRUCTIVE
    # Geschweifte Zuweisung allein ist ein Schreib-, kein Lesevorgang
    assert classify_script("${x} = Get-Date") == ToolClassification.WRITE
    # Ein Cmdlet-Name IN der Klammer wird nicht ausgefuehrt -- kein DESTRUCTIVE
    assert classify_script("${Remove-Item} = 1\nGet-Process") == ToolClassification.WRITE
    # Kein False-Positive: Property-Zugriff im Skriptblock ist kein dynamischer Aufruf
    assert classify_script('Get-Process | Where-Object { $_.Name -eq "x" }') == ToolClassification.READ


def test_native_exec_veto():
    from rag_backend.action_engine.tools.run_powershell_tool import (
        RunPowerShellInput, RunPowerShellTool,
    )

    tool = RunPowerShellTool()
    params = RunPowerShellInput(script="sc stop Spooler")
    detected = tool._classify(params)
    assert detected != ToolClassification.READ
    veto = tool._veto(params, detected)
    assert veto is not None and veto[0] == "native_exec_blocked"
    # pwsh war die Luecke: PowerShell 7 startet eine frische FullLanguage-Shell
    pwsh_params = RunPowerShellInput(script="pwsh -Command Get-Date")
    assert tool._veto(pwsh_params, tool._classify(pwsh_params)) is not None
    # Veto greift auch dann, wenn die Klassifizierung READ ergibt
    assert tool._veto(RunPowerShellInput(script="cmd /c dir"), ToolClassification.READ) is not None
    # READ-Skripte ohne native Programme haben kein Veto
    read_params = RunPowerShellInput(script="Get-Date")
    assert tool._veto(read_params, ToolClassification.READ) is None
    # Kein False-Positive: 'sc' als Pfadsegment ist kein Programmaufruf
    path_params = RunPowerShellInput(script="Get-ChildItem C:\\Users\\sc\\Documents")
    assert tool._veto(path_params, ToolClassification.READ) is None


def test_indented_command_is_not_invisible():
    '''Regression: Kommando-Kopf-Erkennung UND Native-Exec-Veto waren beide auf
    (?:^|[|;&=]\\s*) verankert -- das \\s* galt nur fuer den [|;&=]-Zweig, nicht fuer ^.

    Eine EINGERUECKTE Zeile lieferte damit gar keinen Kopf. Aus
        Get-Date
         powershell -c "..."
    sah das Gate nur 'get-date', stufte READ ein (keine Bestaetigung, sofortige
    Ausfuehrung als Administrator) und das Veto griff ebenfalls nicht -- der
    Kindprozess powershell.exe startet in FullLanguage, also ausserhalb des
    Constrained Language Mode.'''
    from rag_backend.action_engine.tools.run_powershell_tool import (
        RunPowerShellInput, RunPowerShellTool, classify_script,
    )

    tool = RunPowerShellTool()
    for payload in (
        'Get-Date\n powershell -NoProfile -c "Remove-Item C:\\Temp\\x"',
        'Get-ChildItem C:\\\n\tcmd /c "del /s /q C:\\Temp"',
        ' powershell -c "whoami"',
    ):
        params = RunPowerShellInput(script=payload)
        detected = classify_script(payload)
        assert detected != ToolClassification.READ, payload
        assert tool._veto(params, detected) is not None, payload

    # Skriptblock ist Kommandoposition: '%' steht in _READ_COMMANDS.
    block = 'Get-Process | % { powershell -c "whoami" }'
    assert tool._veto(RunPowerShellInput(script=block), classify_script(block)) is not None

    # Programm ueber seinen Pfad: die Namensliste kennt nur System-Binaries.
    exe = 'Get-Date\n .\\mitgebracht.exe'
    assert tool._veto(RunPowerShellInput(script=exe), classify_script(exe)) is not None
    # ... als ARGUMENT bleibt derselbe Pfad erlaubt (sonst waere jedes Listing weg).
    arg = "Get-Item C:\\Program Files\\app\\foo.exe"
    assert tool._veto(RunPowerShellInput(script=arg), ToolClassification.READ) is None


def test_subexpression_is_not_hidden_in_string():
    '''Regression: "$(...)" wird von PowerShell AUSGEFUEHRT, der Klassifizierer hat
    doppelt gequotete Strings aber komplett weggeschnitten. Das Kommando war damit
    unsichtbar -- uebrig blieben zwei harmlose Koepfe, also READ.'''
    from rag_backend.action_engine.tools.run_powershell_tool import (
        RunPowerShellInput, RunPowerShellTool, classify_script,
    )

    tool = RunPowerShellTool()
    for payload in (
        'Get-Date; Write-Output "$(cmd /c del C:\\Temp\\x)"',
        'Get-Date; $(cmd /c whoami)',
    ):
        params = RunPowerShellInput(script=payload)
        detected = classify_script(payload)
        assert detected != ToolClassification.READ, payload
        assert tool._veto(params, detected) is not None, payload

    # Einfache Quotes sind in PowerShell literal -- keine Expansion, kein Fund.
    assert classify_script("Get-Content 'C:\\Argus_Workspace\\$(x).txt'") == ToolClassification.READ


def test_state_changing_method_call_is_not_read():
    '''Regression: Methodenaufruf an der Cmdlet-Erkennung vorbei. Beide Koepfe von
        Get-WmiObject -List Win32_Process | % { $_.Create('calc') }
    stehen in der READ-Allowlist ('%' ist ForEach-Object) -- die Methodenliste kannte
    aber nur loeschende Verben, also lief der Prozess-Start als READ durch.'''
    from rag_backend.action_engine.tools.run_powershell_tool import classify_script

    assert classify_script("Get-WmiObject -List Win32_Process | % { $_.Create('calc') }") == ToolClassification.DESTRUCTIVE
    assert classify_script("Get-Service | % { $_.Stop() }") == ToolClassification.DESTRUCTIVE
    # Lesende Methoden bleiben READ -- die Klammer muss direkt am Namen stehen.
    assert classify_script('Get-Process | Where-Object { $_.Name.StartsWith("chrome") }') == ToolClassification.READ
    assert classify_script('Get-Service | Where-Object { $_.Name.Contains("sql") }') == ToolClassification.READ


def test_file_content_read_outside_workspace_needs_confirmation():
    '''Datei-INHALTE ausserhalb der Ops-Pfade sind bestaetigungspflichtig (nicht
    blockiert). Die _BLOCKED_PATTERNS decken nur bekannte Secret-Dateinamen ab --
    ein beliebiges Dokument faellt nicht darunter und lief als READ unbestaetigt
    mit Admin-Rechten, bis zu 20k Zeichen zurueck ins Modell.'''
    from rag_backend.action_engine.tools.run_powershell_tool import classify_script

    assert classify_script("Get-Content C:\\Users\\Someone\\Documents\\vertrag.txt") == ToolClassification.WRITE
    assert classify_script('Select-String -Path "D:\\Projekte\\notizen.md" -Pattern x') == ToolClassification.WRITE
    # Pfad ueberhaupt nicht erkennbar (Variable, Pipe-Eingang) -> fail-closed
    assert classify_script("Get-Content $pfad") == ToolClassification.WRITE
    assert classify_script("Get-ChildItem C:\\Windows\\Logs | Select-String fehler") == ToolClassification.WRITE
    # Workspace bleibt unbestaetigt lesbar -- das ist das Gedaechtnis des Agenten.
    assert classify_script("Get-Content C:\\Argus_Workspace\\Temp\\notiz.txt") == ToolClassification.READ
    assert classify_script("Get-ChildItem C:\\Argus_Workspace | Select-String fehler") == ToolClassification.READ
    # Objekt-lesende Ops-Befehle sind nicht betroffen
    assert classify_script("Get-Service | Where-Object Status -eq Running") == ToolClassification.READ
    assert classify_script("Get-ChildItem C:\\Windows\\Logs") == ToolClassification.READ


def test_confirmation_is_bound_to_requester():
    '''Ueber HTTP darf nur der Urheber freigeben. Vorher genuegte irgendein gueltiges
    Token -- auch der Service-Token aus der .env.'''
    import uuid
    from rag_backend.action_engine.confirmation import ConfirmationStore, OWNER_CHANNEL

    store = ConfirmationStore()
    req = store.create(
        tool_name="run_powershell", tool_class="write", parameters={},
        impact_description="test", requested_by=7,
    )
    assert store.approve(req.id, actor=9) is None            # fremder Nutzer
    assert store.list_pending(actor=9) == []                 # sieht die ID nicht einmal
    assert any(r.id == req.id for r in store.list_pending(actor=7))
    approved = store.approve(req.id, actor=7)
    assert approved is not None and approved.status.value == "approved"

    # Telegram bleibt Besitzer-Kanal (Chat-ID-Pruefung ist dort die Auth).
    req2 = store.create(
        tool_name="run_powershell", tool_class="write", parameters={},
        impact_description="test", requested_by=7,
    )
    assert store.approve(req2.id, actor=OWNER_CHANNEL) is not None

    # Ohne bekannten Urheber ist ueber HTTP niemand zustaendig (fail-closed).
    req3 = store.create(
        tool_name="run_powershell", tool_class="write", parameters={},
        impact_description="test",
    )
    assert store.approve(req3.id, actor=0) is None
    assert store.approve(req3.id, actor=OWNER_CHANNEL) is not None


# ---- Aufgaben-Freigabe (Auto-Modus) -----------------------------------------
# Eine Aufgabe fragt EINMAL; loeschende Aktionen bleiben ausgenommen.

@pytest.fixture(autouse=True)
def _reset_context_vars():
    # ContextVars ueberleben sonst den Test und faerben den naechsten ein.
    import rag_backend.action_engine.base_tool as bt
    token_task = bt.CURRENT_TASK.set(None)
    token_user = bt.CURRENT_USER_ID.set(None)
    yield
    bt.CURRENT_TASK.reset(token_task)
    bt.CURRENT_USER_ID.reset(token_user)


@pytest.fixture
def task_store(tmp_path, monkeypatch):
    # Store auf ein frisches Verzeichnis umbiegen -- /tmp waere zwischen Tests geteilt.
    import rag_backend.action_engine.confirmation as conf_mod
    monkeypatch.setattr(conf_mod, "STATE_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(conf_mod, "TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(conf_mod, "LOCK_FILE", str(tmp_path / "conf.lock"))
    monkeypatch.setattr(conf_mod.FileLock, "__init__",
                        lambda self, path=str(tmp_path / "conf.lock"): (
                            setattr(self, "path", path), setattr(self, "fd", None))[0])
    return conf_mod


def _set_task(task_id="task-1", channel="dashboard", text="installiere LibreOffice"):
    import rag_backend.action_engine.base_tool as bt
    bt.CURRENT_TASK.set({"id": task_id, "text": text, "channel": channel})


async def test_task_approval_second_write_skips_gate(monkeypatch, audit_calls, task_store):
    _patch_secrets(monkeypatch, telegram_configured=True)
    created = []

    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = "installiere LibreOffice"
        requested_by = 0

    def _create(**kwargs):
        created.append(kwargs)
        return _Req()

    async def _wait(_id):
        class _R:
            status = task_store.ConfirmationStatus.APPROVED
        # Freigabe wirkt aufgabenweit -- genau das schreibt _mark_task fest.
        task_store.confirmation_store._mark_task(_Req(), "approved")
        return _R()

    monkeypatch.setattr(task_store.confirmation_store, "create", _create)
    monkeypatch.setattr(task_store.confirmation_store, "wait_for_resolution", _wait)

    _set_task()
    first = _DummyTool(ToolClassification.WRITE)
    assert (await first.run({}, user_id=0)).success is True
    assert first.executed is True
    assert len(created) == 1, "erste schreibende Aktion muss fragen"

    # Zweiter Schritt derselben Aufgabe: KEINE neue Anfrage.
    second = _DummyTool(ToolClassification.WRITE)
    assert (await second.run({}, user_id=0)).success is True
    assert second.executed is True
    assert len(created) == 1, "zweite Aktion derselben Aufgabe darf nicht erneut fragen"
    assert any(c["status"] == "task_approved_skip" for c in audit_calls)


async def test_destructive_gated_despite_task_approval(monkeypatch, audit_calls, task_store):
    _patch_secrets(monkeypatch, telegram_configured=True)
    _set_task()
    # Aufgabe ist bereits freigegeben ...
    class _Approved:
        id = "req-0"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    task_store.confirmation_store._mark_task(_Approved(), "approved")

    created = []

    def _create(**kwargs):
        created.append(kwargs)
        class _R:
            id = "req-del"
        return _R()

    async def _wait(_id):
        class _R:
            status = task_store.ConfirmationStatus.APPROVED
        return _R()

    monkeypatch.setattr(task_store.confirmation_store, "create", _create)
    monkeypatch.setattr(task_store.confirmation_store, "wait_for_resolution", _wait)

    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    assert (await tool.run({}, user_id=0)).success is True
    # ... trotzdem wurde gefragt, und zwar OHNE Aufgaben-Bindung.
    assert len(created) == 1, "loeschende Aktion muss trotz Aufgaben-Freigabe fragen"
    assert created[0]["task_scope"] is False


async def test_task_approval_expires(monkeypatch, task_store):
    _set_task()
    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    store = task_store.confirmation_store
    store._mark_task(_Req(), "approved")
    assert store.task_status("task-1") == "approved"
    # Zeitdeckel abgelaufen -> Freigabe ist weg, es wird wieder gefragt.
    monkeypatch.setattr(store, "_task_ttl", -1)
    store._mark_task(_Req(), "approved")
    assert store.task_status("task-1") is None


async def test_task_reject_blocks_followup_writes(monkeypatch, audit_calls, task_store):
    _patch_secrets(monkeypatch, telegram_configured=True)
    _set_task()
    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    task_store.confirmation_store._mark_task(_Req(), "rejected")

    created = []
    monkeypatch.setattr(task_store.confirmation_store, "create",
                        lambda **kw: created.append(kw))

    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is False and tool.executed is False
    assert not created, "nach Ablehnung darf keine neue Anfrage entstehen"
    assert "rejected this task" in (out.error or "")
    assert audit_calls[-1]["status"] == "confirmation_task_rejected"


async def test_chat_channel_allows_confirmation_without_telegram(monkeypatch, audit_calls, task_store):
    # Ohne Telegram, aber mit offener Chat-Karte: die Freigabe ist moeglich.
    _patch_secrets(monkeypatch, telegram_configured=False)
    _set_task(channel="dashboard")
    _patch_confirmation(monkeypatch, task_store.ConfirmationStatus.APPROVED)
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is True and tool.executed is True
    assert not any(c["status"] == "confirmation_no_channel" for c in audit_calls)


async def test_webui_channel_still_needs_telegram(monkeypatch, audit_calls, task_store):
    # Regression: der OpenAI-Proxy kann keine Karte zeigen -- dort bleibt es fail-closed.
    _patch_secrets(monkeypatch, telegram_configured=False)
    _set_task(channel="webui")
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


def test_resolution_writes_audit(monkeypatch, audit_calls, task_store):
    # Jede Entscheidung landet im Audit.
    store = task_store.ConfirmationStore()
    req = store.create(tool_name="run_powershell", tool_class="write", parameters={},
                       impact_description="test", requested_by=0,
                       task_id="task-9", task_scope=True, task_description="test")
    store.approve(req.id, actor=0, via="chat")
    entry = [c for c in audit_calls if c.get("tool") == "confirmation"]
    assert entry, "Freigabe muss einen Audit-Eintrag erzeugen"
    assert entry[-1]["status"] == "approved"
    assert '"via": "chat"' in entry[-1]["details"]
"""


# ---------------------------------------------------------------------------
# Main-Logik
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _log("--- Starte Setup3: Action-Engine & Tools ---")

    #--- Credentials laden: API_Tokens/*.txt > Shell-ENV > .env
    try:
        from dotenv import dotenv_values as _dv
        _dv_available = True
    except ImportError:
        _dv = None
        _dv_available = False

    _existing_env: dict = {}
    if _dv_available:
        try:
            _existing_env = _dv(BASE_DIR / ".env") if (BASE_DIR / ".env").exists() else {}
        except Exception:
            pass

    _api_dir = BASE_DIR / "API_Tokens"
    _api_dir.mkdir(parents=True, exist_ok=True)
    _api_env: dict = {}
    if _api_dir.exists():
        for _txt in sorted(_api_dir.glob("*.txt")):
            if _txt.stem in ("fernet_key", "postgres_password"):
                continue
            try:
                if _dv_available:
                    _parsed = _dv(_txt)
                else:
                    _parsed = {}
                if _parsed:
                    # Nur echte ENV-artige Keys uebernehmen: dotenv parst auch
                    # ENC:-Ciphertext als 'Key'.
                    _api_env.update({k: v for k, v in _parsed.items()
                                     if v and k.replace("_", "").isalnum()})
                else:
                    _raw = _txt.read_text(encoding="utf-8").strip()
                    if "=" in _raw:
                        for _line in _raw.splitlines():
                            if "=" in _line:
                                _k2, _, _v2 = _line.partition("=")
                                if _k2.strip() and _v2.strip():
                                    _api_env[_k2.strip()] = _v2.strip()
                    elif _raw:
                        _api_env[_txt.stem] = _raw
            except Exception:
                pass

    _log(f"API_Tokens geladen: {list(_api_env.keys())}")

    def _safe_env(key: str, default: str = "") -> str:
        #--- API_Tokens hat IMMER Vorrang vor .env (bricht Leer-Wert-Zyklus)
        return (
            _api_env.get(key)
            or os.getenv(key)
            or _existing_env.get(key)
            or default
        )

    #--- SSH-Credentials werden von setup_ssh.ps1 in dedizierten Dateien gepflegt
    _ssh_user_path = _api_dir / "ssh_user.txt"
    _ssh_password_path = _api_dir / "ssh_password.txt"
    _ssh_key_path = _api_dir / "ssh_key"

    def _read_credential(path):
        """Liest eine Credential-Datei und unterscheidet 'fehlt' von 'nicht lesbar'.

        Beide Seiten haerten API_Tokens/ auf owner-only (Python via icacls,
        setup_ssh.ps1 via Set-Acl). Laufen sie unter verschiedenen Konten, ist die
        Datei zwar befuellt, aber fuer diesen Prozess unlesbar -- Setup3 meldete dann
        "nicht gefunden" und schickte einen auf die falsche Faehrte."""
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8-sig").strip()
        except PermissionError:
            _log(f"ABBRUCH: {path} existiert, ist aber nicht LESBAR (Windows-ACL).")
            _log("   API_Tokens/ ist owner-only gehaertet. Vermutlich lief setup_ssh.ps1")
            _log("   unter einem anderen Konto als dieses Skript -- beide unter demselben")
            _log("   Benutzer starten oder API_Tokens/ per icacls fuer dieses Konto freigeben.")
            import sys as _sys
            _sys.exit(1)
        except OSError as e:
            _log(f"ABBRUCH: {path} nicht lesbar: {e}")
            import sys as _sys
            _sys.exit(1)

    _ssh_user = _read_credential(_ssh_user_path)
    _ssh_password = _read_credential(_ssh_password_path)
    _ssh_key = _read_credential(_ssh_key_path)

    #--- Key ODER Passwort genuegt -- je nachdem, ob der Host schon umgestellt ist.
    #--- Ohne Rechnersteuerung braucht es keins von beidem.
    if ACTION_ENGINE_ENABLED != "true":
        _log("Rechnersteuerung AUS (ACTION_ENGINE_ENABLED=false): SSH-Zugangsdaten werden nicht geprueft.")
    elif not _ssh_user or not (_ssh_key or _ssh_password):
        _log("ABBRUCH: SSH-Credentials fehlen oder sind leer.")
        _log(f"   Erwartet: {_ssh_user_path}")
        _log(f"   Erwartet: {_ssh_key_path} (Public-Key-Verfahren)")
        _log(f"   oder:     {_ssh_password_path} (Passwort-Fallback)")
        _log("   setup_ssh.ps1 zuerst als Administrator auf dem Windows-Host ausfuehren.")
        import sys as _sys
        _sys.exit(1)

    if ACTION_ENGINE_ENABLED != "true":
        pass
    elif _ssh_key:
        _log("SSH-Auth: Public-Key (API_Tokens/ssh_key).")
    else:
        _log("HINWEIS: SSH-Auth laeuft noch ueber das Passwort. setup_ssh.ps1 erneut")
        _log("   ausfuehren stellt auf Public-Key um und leert die Passwort-Datei.")

    _telegram_secrets = {
        "TELEGRAM_BOT_TOKEN": "telegram.txt",
        "TELEGRAM_CHAT_ID":  "telegram.txt",
    }
    _telegram_template = "TELEGRAM_BOT_TOKEN=<BOT_TOKEN_VOM_BOTFATHER>\nTELEGRAM_CHAT_ID=<DEINE_CHAT_ID>"
    _missing_tg = [k for k in _telegram_secrets if not _safe_env(k)]
    if _missing_tg:
        _log("HINWEIS: Folgende Telegram-Credentials fehlen:")
        for _k in _missing_tg:
            _log(f"   {_k}")
        _fpath = _api_dir / "telegram.txt"
        if not _fpath.exists():
            _fpath.write_text(_telegram_template + "\n", encoding="utf-8")
            _log("   Placeholder-Datei angelegt: API_Tokens/telegram.txt -- bitte befuellen.")

    _placeholder_tokens = [
        v.split("=", 1)[1] for v in _telegram_template.splitlines() if "=" in v
    ]
    for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        _v = _safe_env(_k)
        if _v and _v in _placeholder_tokens:
            _log(f"ABBRUCH: {_k} enthaelt noch Platzhalter ({_v}) -- bitte API_Tokens/telegram.txt befuellen.")
            import sys as _sys
            _sys.exit(1)

    #--- SSH_USER/SSH_KEY/SSH_PASSWORD kommen per Volume-Mount in den Container,
    #--- nicht ueber die .env.

    #--- MCP_SERVERS-Default lebt im mcp_loader. In die .env kommt der Schluessel
    #--- nur bei einem echten Override.
    _mcp_default = (
        '[{"name":"fs","command":"python","args":["-m","rag_backend.mcp_servers.filesystem"]}]'
    )
    _mcp_custom = (_safe_env("MCP_SERVERS") or "").strip()

    ACTION_CONFIG = {
        "SSH_HOST": "host.docker.internal",
        "SSH_PORT": "22",
        "SSH_TIMEOUT": "30",
        "ACTION_ENGINE_ENABLED": ACTION_ENGINE_ENABLED,
        # TELEGRAM_BOT_TOKEN/CHAT_ID stehen NICHT in der .env -- der Container
        # liest sie via load_secret aus /run/secrets/telegram.
        "TELEGRAM_HISTORY_LIMIT": _safe_env("TELEGRAM_HISTORY_LIMIT", "12"),
        #--- Sicherheits-/Betriebsschalter der Action-Engine. Die Werte SIND die
        #--- Code-Defaults und stehen hier, damit man sie in der .env findet.
        #    Known-Hosts-Datei der SSH-Verbindung (TOFU beim ersten Connect):
        "SSH_KNOWN_HOSTS": _safe_env("SSH_KNOWN_HOSTS", "/host/argus_workspace/.argus_known_hosts"),
        #    Freigabe-Dialog fuer schreibende Tools: Wartezeit und Sperre nach Ablehnung.
        "CONFIRMATION_TIMEOUT_SECONDS": _safe_env("CONFIRMATION_TIMEOUT_SECONDS", "120"),
        "CONFIRMATION_COOLDOWN_SECONDS": _safe_env("CONFIRMATION_COOLDOWN_SECONDS", "30"),
        #    Aufgaben-Freigabe: gilt fuer alle schreibenden Schritte EINER Aufgabe.
        #    Dieser Wert ist der Zeitdeckel, nicht die normale Lebensdauer.
        "TASK_APPROVAL_TTL_SECONDS": _safe_env("TASK_APPROVAL_TTL_SECONDS", "1800"),
        #    Kappungsgrenzen gegen Kontext-Flutung durch Tool-Ausgaben.
        "POWERSHELL_MAX_OUTPUT_CHARS": _safe_env("POWERSHELL_MAX_OUTPUT_CHARS", "20000"),
        "FS_READ_MAX_CHARS": _safe_env("FS_READ_MAX_CHARS", "20000"),
        #    Remote-MCP-Server default-deny. Leer lassen, solange kein externer
        #    MCP-Server gebraucht wird.
        "MCP_ALLOWED_URLS": _safe_env("MCP_ALLOWED_URLS", ""),
    }
    if _mcp_custom and _mcp_custom != _mcp_default:
        ACTION_CONFIG["MCP_SERVERS"] = _mcp_custom
    write_env_block("SETUP3", ACTION_CONFIG, BASE_DIR / ".env")

    #--- Migration: alte WINRM_*- und SSH_*-Eintraege ausserhalb des SETUP3-Blocks
    #--- entfernen.
    if prune_env_keys(
        {
            "WINRM_USER", "WINRM_PASSWORD", "WINRM_HOST", "WINRM_PORT",
            "WINRM_USE_SSL", "WINRM_AUTH", "WINRM_ENCRYPTION",
            "SSH_USER", "SSH_PASSWORD",
        },
        BASE_DIR / ".env",
    ):
        _log("Migration: alte WinRM- und SSH-Eintraege ausserhalb des Blocks aus .env entfernt.")

    ae_dir = BASE_DIR / "rag_backend" / "action_engine"
    tools_dir = ae_dir / "tools"
    ae_dir.mkdir(parents=True, exist_ok=True)
    tools_dir.mkdir(parents=True, exist_ok=True)

    #--- Action-Engine Core
    writefile(ae_dir / "__init__.py", AE_INIT_PY, "action_engine/__init__.py")
    p_winrm = ae_dir / "winrm_client.py"
    if p_winrm.exists():
        try:
            p_winrm.unlink()
            _log("Alten winrm_client.py geloescht.")
        except Exception as e:
            _log(f"WARNUNG: Konnte alten winrm_client.py nicht loeschen: {e}")
    writefile(ae_dir / "ssh_client.py", SSH_CLIENT_PY, "ssh_client.py")
    writefile(ae_dir / "registry.py", REGISTRY_PY, "registry.py")
    writefile(ae_dir / "base_tool.py", BASE_TOOL_PY, "base_tool.py")
    writefile(ae_dir / "audit.py", AUDIT_PY, "audit.py")
    writefile(ae_dir / "confirmation.py", CONFIRMATION_PY, "confirmation.py")

    #--- Read-Tools
    writefile(tools_dir / "disk_space.py", DISK_SPACE_PY, "tools/disk_space.py")
    writefile(tools_dir / "windows_updates.py", WINDOWS_UPDATES_PY, "tools/windows_updates.py")
    writefile(tools_dir / "eventlog.py", EVENTLOG_PY, "tools/eventlog.py")

    #--- Write-Tools
    writefile(tools_dir / "windows_update_install.py", WINDOWS_UPDATE_INSTALL_PY, "tools/windows_update_install.py")
    writefile(tools_dir / "restart_service.py", RESTART_SERVICE_PY, "tools/restart_service.py")

    #--- Generisches PowerShell-Tool
    writefile(tools_dir / "run_powershell_tool.py", RUN_POWERSHELL_TOOL_PY, "tools/run_powershell_tool.py")

    #--- tools/__init__.py (Registrierung)
    writefile(tools_dir / "__init__.py", TOOLS_INIT_PY, "tools/__init__.py")

    #--- Duplikate entfernen
    for leftover in ["read_eventlog.py", "winget_updates.py", "winget_install.py"]:
        p2 = tools_dir / leftover
        if p2.exists():
            p2.unlink()
            _log(f"DELETED Duplikat: {p2}")

    #--- Telegram-Bot & MCP-Loader
    writefile(BASE_DIR / "rag_backend" / "telegram_bot.py", TELEGRAM_BOT_PY, "telegram_bot.py")
    writefile(BASE_DIR / "rag_backend" / "mcp_loader.py", MCP_LOADER_PY, "mcp_loader.py")

    #--- Migration: path_guard.py loeschen. Der aktive Pfad-Schutz lebt in
    #    mcp_servers/filesystem.py (_enforce_whitelist).
    p_path_guard = ae_dir / "path_guard.py"
    if p_path_guard.exists():
        try:
            p_path_guard.unlink()
            _log("Alten path_guard.py geloescht (WinRM-Altlast).")
        except Exception as e:
            _log(f"WARNUNG: Konnte alten path_guard.py nicht loeschen: {e}")

    #--- MCP-Filesystem-Server (Mini 1)
    mcp_servers_dir = BASE_DIR / "rag_backend" / "mcp_servers"
    mcp_servers_dir.mkdir(parents=True, exist_ok=True)
    writefile(mcp_servers_dir / "__init__.py", MCP_FILESYSTEM_INIT_PY, "mcp_servers/__init__.py")
    writefile(mcp_servers_dir / "filesystem.py", MCP_FILESYSTEM_PY, "mcp_servers/filesystem.py")

    #--- Offline-Tests fuer Confirmation-Flow + Skript-Klassifizierung
    writefile(BASE_DIR / "rag_backend" / "tests" / "test_action_engine.py",
              TEST_ACTION_ENGINE_PY, "tests/test_action_engine.py")

    if verify_markers:
        verify_markers(BASE_DIR)

    _log("")
    _log("Setup3 abgeschlossen.")
    #--- Setup4 ist der naechste Generator, NICHT der Build. Der Build gehoert
    #--- ans Ende der Kette.
    _log("Naechster Schritt: python Setup4.py")
