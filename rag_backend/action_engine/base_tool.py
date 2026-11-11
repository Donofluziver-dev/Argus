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


#--- Rangfolge fuer "nur anheben": eine zweite Meinung darf nie entschaerfen.
_CLASS_RANK = {
    ToolClassification.READ: 0,
    ToolClassification.WRITE: 1,
    ToolClassification.DESTRUCTIVE: 2,
    ToolClassification.BLOCKED: 3,
}


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

#--- Rolle des Akteurs. Host- und Cloud-Werkzeuge sind Administratoren
#--- vorbehalten; ein selbst registriertes Konto (Rolle 'chat') darf sie nicht
#--- ausloesen, auch nicht ueber einen Umweg im Agenten. None = interner Aufruf
#--- ohne Konto (Hintergrundjobs).
CURRENT_USER_ROLE: ContextVar[str | None] = ContextVar("argus_current_user_role", default=None)

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

    Fail-open und bewusst breit gefangen: ohne Runnable-Kontext (Unit-Tests) gibt
    es keinen Konsumenten. Das Gate selbst funktioniert auch stumm --
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
    #--- Alle Action-Engine-Werkzeuge greifen auf den Host oder Dritte zu.
    admin_only: bool = True
    #--- Darf eine Freigabe fuer die ganze Aufgabe gelten? Nein bei Werkzeugen,
    #--- deren Eingabe beliebiger Code ist: Dort waere die erste Freigabe ein
    #--- Blankoscheck fuer jedes weitere Skript derselben Aufgabe.
    task_scoped_approval: bool = True

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

    async def _refine_classification(self, params: BaseModel,
                                     detected: ToolClassification) -> ToolClassification:
        # Zweite Meinung NACH _classify (z. B. ein echter Parser). Darf die
        # Einstufung nur anheben, nie senken.
        return detected

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
        # Freigegeben wird ueber die Karte im Chat, und nur vom ausloesenden Konto.
        # Ohne Chat (Hintergrundjob) oder ohne Konto koennte niemand freigeben:
        # dann sofort absagen, statt bis zum Zeitlimit zu warten.
        if task.get("channel") != "dashboard" or user_id is None:
            _audit("confirmation_no_channel")
            return ToolOutput(
                success=False,
                error=(
                    "Operation blocked: this action needs an approval, and approvals are "
                    "given on the card in the Argus chat. Run the task from the Argus chat."
                ),
            )
        impact = self._impact_description_for(validated, detected)
        if task_scope:
            impact = (
                f"AUFGABE: {task.get('text') or '(ohne Beschreibung)'}\n"
                f"Diese Freigabe erlaubt weitere schreibende Schritte mit '{self.name}' "
                "in dieser Aufgabe. Andere Werkzeuge, loeschende Aktionen und Eingriffe "
                "in Autostart, Dienste, Registry oder Konten fragen weiterhin einzeln nach.\n\n"
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

        role = CURRENT_USER_ROLE.get()
        if self.admin_only and role is not None and role != "admin":
            return ToolOutput(
                success=False,
                error=f"'{self.name}' is reserved for administrators. Tell the user that "
                      "this action needs an administrator account.",
            )

        input_schema = self.get_input_schema()
        validated = input_schema(**params)
        #--- Koeder-Token in den Parametern = ein gekaperter Agent will gelesene
        #--- Geheimnisse ueber ein Werkzeug hinausschaffen.
        from rag_backend.canary import check_canary
        if check_canary(validated.model_dump_json(), f"Werkzeug {self.name}"):
            return ToolOutput(success=False, error="Action blocked: the parameters contain protected data.")
        input_hash = hashlib.sha256(validated.model_dump_json().encode()).hexdigest()[:16]
        detected = self._classify(validated)
        refined = await self._refine_classification(validated, detected)
        if _CLASS_RANK.get(refined, 0) > _CLASS_RANK.get(detected, 0):
            detected = refined
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

        from rag_backend.egress_guard import (
            EGRESS_TOOLS,
            EgressCheckedScope,
            SENSITIVE_SOURCE_TOOLS,
            check_egress_allowed,
            mark_sensitive_source,
        )
        if self.name in EGRESS_TOOLS:
            egress_block = await check_egress_allowed(
                self.name,
                validated.model_dump(),
                delegate_confirm_to_caller=True,
            )
            if egress_block is not None:
                return ToolOutput(success=False, error=egress_block)

        if self._requires_confirmation(detected):
            task = CURRENT_TASK.get() or {}
            task_id = task.get("id")
            #--- LOESCHENDE Aktionen bleiben immer einzeln bestaetigungspflichtig,
            #--- auch innerhalb einer freigegebenen Aufgabe. Das begrenzt, was eine
            #--- per Prompt-Injection gekaperte Aufgabe anrichten kann.
            is_destructive = detected == ToolClassification.DESTRUCTIVE
            #--- Aufgabenweite Freigabe nur, wenn es ueberhaupt eine Aufgabe gibt
            #--- und das Werkzeug sie zulaesst.
            task_scope = bool(task_id) and not is_destructive and self.task_scoped_approval

            if task_scope:
                from rag_backend.action_engine.confirmation import confirmation_store
                #--- Die Aufgaben-Freigabe gilt nur fuer DASSELBE Werkzeug: eine
                #--- im selben Turn eingeschleuste Aktion mit einem anderen Werkzeug
                #--- fragt neu.
                prior = confirmation_store.task_status(task_id, self.name)
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
                        prior = confirmation_store.task_status(task_id, self.name)
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
            if self.name in EGRESS_TOOLS:
                with EgressCheckedScope():
                    result = await self._execute(validated)
            else:
                result = await self._execute(validated)
            if self.name in SENSITIVE_SOURCE_TOOLS and result.success and result.data:
                mark_sensitive_source(self.name, str(result.data))
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

    def _impact_description_for(self, params: BaseModel,
                                detected: ToolClassification) -> str:
        # Wie _impact_description, kennt aber die effektive Einstufung (nach
        # _refine_classification). Werkzeuge ohne eigene Stufen-Anzeige ignorieren sie.
        return self._impact_description(params)

    def to_langchain_tool(self):
        from langchain_core.tools import StructuredTool

        input_schema = self.get_input_schema()
        tool_instance = self

        async def _afunc(**kwargs) -> str:
            result = await tool_instance.run(kwargs, user_id=CURRENT_USER_ID.get())
            if result.success:
                return str(result.data)
            if result.error and result.error.startswith("Websuche und andere Zugriffe nach aussen"):
                return result.error
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
