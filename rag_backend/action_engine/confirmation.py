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
    #--- Wer die Aktion ausgeloest hat. NUR dieser Akteur darf sie freigeben.
    requested_by: Optional[int] = None
    #--- Aufgaben-Freigabe: eine Aufgabe = ein Agenten-Turn. Die ERSTE schreibende
    #--- Aktion fragt fuer das ganze Vorhaben. task_scope bleibt bei LOESCHENDEN
    #--- Aktionen False -- die fragen immer einzeln.
    task_id: Optional[str] = None
    task_scope: bool = False
    task_description: str = ""


def _actor_may_resolve(req: "ConfirmationRequest", actor: Optional[int]) -> bool:
    '''Darf dieser Akteur die Confirmation aufloesen (freigeben/ablehnen/sehen)?

    Nur der Urheber. Eine Anfrage ohne requested_by ist nicht freigebbar
    (fail-closed). Den frueheren Besitzer-Kanal ohne Konto (Telegram) gibt es
    nicht mehr; jeder Aufrufer nennt das Konto, unter dem er handelt.'''
    if actor is None or req.requested_by is None:
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

    def task_status(self, task_id: Optional[str], tool_name: Optional[str] = None) -> Optional[str]:
        '''"approved" | "rejected" | None fuer diese Aufgabe.

        Mit tool_name gilt eine Freigabe nur fuer DASSELBE Werkzeug, mit dem sie
        erteilt wurde (Eintraege ohne Werkzeug zaehlen dann nicht). Eine Ablehnung
        gilt fuer die ganze Aufgabe.

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
        if not entry:
            return None
        status = entry.get("status")
        if status == "approved" and tool_name is not None and entry.get("tool") != tool_name:
            return None
        return status

    def _mark_task(self, req: "ConfirmationRequest", status: str) -> None:
        '''Ergebnis einer aufgabenweiten Freigabe festhalten.

        EINZIGER Schreibort -- aufgerufen aus approve()/reject(). Dadurch wirkt es
        identisch, egal ob die Entscheidung ueber die Chat-Karte oder den
        Login-Endpunkt kam; kein Aufrufer muss etwas zusaetzlich tun.'''
        if not (req.task_scope and req.task_id):
            return
        with FileLock():
            tasks = _load_tasks()
            tasks[req.task_id] = {
                "status": status,
                "tool": req.tool_name,
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
        self._pop_expired()

    async def _clean_expired_async(self):
        self._pop_expired()

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

        #--- Entschieden wird ueber die Freigabe-Karte im Chat (Argus Chat, Argus Code).
        log.info(f"Confirmation erstellt: {req.id} fuer {tool_name}")
        return req

    def list_pending(self, *, actor: Optional[int]) -> list[ConfirmationRequest]:
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
                user_id=actor,
                details=json.dumps({
                    "id": req.id, "tool_name": req.tool_name,
                    "task_scope": req.task_scope, "task_id": req.task_id,
                    "via": via,
                }, ensure_ascii=False),
            )
        except Exception as e:
            log.warning(f"Audit der Freigabe-Entscheidung fehlgeschlagen: {e}")

    def approve(self, confirmation_id: str, *,
                actor: Optional[int],
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

    def confirm_after_cooldown(self, confirmation_id: str, *,
                               actor: Optional[int],
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

    def reject(self, confirmation_id: str, *,
               actor: Optional[int],
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
            # Auch das Verstreichenlassen ist eine Entscheidung und gehoert ins Log.
            self._audit_decision(timed_out, "timeout", None, "expired")
            return timed_out
        return None


confirmation_store = ConfirmationStore()
