'''Cloud-Missions-Tools (Setup4) -- BaseTool-Hooks, KEIN run()-Override.

start_mission ist WRITE-klassifiziert: die Erst-Freigabe laeuft ueber den
bestehenden Confirmation-Flow (Karte im Chat); _impact_description zeigt das
deterministisch redigierte Paket als Vorschau. Nach Freigabe laufen zusaetzlich
Qwen-Redaction-Pass und PII-Tripwire vor jedem tatsaechlichen Versand.
'''
import asyncio
import os

from pydantic import BaseModel, Field

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput


def _auto_approve() -> bool:
    # Default FALSE: Daten verlassen das System, daher standardmäßig Bestätigung nötig.
    # Wer die Freigabe überspringen will, setzt CLOUD_MISSION_AUTO_APPROVE=true.
    # Gilt fuer start_mission UND cloud_ask: die Eskalation nach wiederholten
    # Fehlschlaegen ruft cloud_ask selbststaendig auf und darf nicht an einer
    # Freigabe haengen, die niemand sieht. Redaction, Tripwire und Canary laufen
    # in beiden Faellen trotzdem vor jedem Versand.
    # In markierten Gespraechen (mit vertraulichen Daten) wird NIEMALS automatisch
    # freigegeben, auch nicht mit CLOUD_MISSION_AUTO_APPROVE=true.
    try:
        from rag_backend.egress_guard import is_conversation_sensitive
        if is_conversation_sensitive():
            return False
    except Exception:
        pass
    return os.getenv("CLOUD_MISSION_AUTO_APPROVE", "false").lower() == "true"


class StartMissionInput(BaseModel):
    goal: str = Field(
        ..., min_length=10, max_length=4000,
        description="Mission goal, e.g. 'Research X and produce a structured analysis with sources'. The mission fetches current facts itself via local web search.",
    )
    context: str = Field(
        default="", max_length=20000,
        description="Task-relevant context (facts, requirements, text excerpts). NO real names, addresses or contact data -- redaction runs additionally.",
    )
    provider: str = Field(
        default="", pattern="^(|gemini)$",
        description="Optional: 'gemini'. Empty = default provider.",
    )


class StartMissionTool(BaseTool):
    name = "start_mission"
    description = (
        "Starts a background mission: cloud agents work in loops (planning -> "
        "local web research -> drafting -> review) on a larger research, "
        "analysis or write-up. The mission fetches current facts itself and "
        "cites sources; only redacted, pseudonymized data leaves the system. "
        "The result is delivered automatically on completion into the chat it "
        "was started from; progress via mission_status. Start directly -- parallel "
        "missions are allowed. For questions needing an IMMEDIATE answer in "
        "chat use web_search/deep_research or cloud_ask instead. Phrase "
        "goal/context without personal data (names, addresses, contacts)."
    )
    classification = ToolClassification.WRITE
    timeout_seconds = 180

    def get_input_schema(self) -> type[BaseModel]:
        return StartMissionInput

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        if _auto_approve():
            return False
        return True

    def _impact_description(self, params: BaseModel) -> str:
        from rag_backend.cloud_agents import redactor
        red_goal, mapping = redactor.redact_deterministic(params.goal)
        red_ctx, mapping = redactor.redact_deterministic(params.context or "", mapping)
        preview = red_goal + (("\n" + red_ctx) if red_ctx else "")
        if len(preview) > 900:
            preview = preview[:900] + " [...]"
        return (
            "Cloud-Mission starten -- dieses redigierte Paket wird an eine externe "
            "KI (Gemini) übertragen:\n\n" + preview +
            "\n\n(Regex-Vorschau. Vor dem Versand läuft zusätzlich der Qwen-NER-Pass, "
            "der auch unbekannte Namen erkennt, sowie der PII-Tripwire.)"
        )

    async def _execute(self, params: StartMissionInput) -> ToolOutput:
        # Kompletter Start-Flow lebt zentral in missions.launch_mission.
        from rag_backend.cloud_agents.missions import launch_mission
        ok, msg = await launch_mission(params.goal, params.context, params.provider)
        if not ok:
            return ToolOutput(success=False, error=msg)
        return ToolOutput(success=True, data=msg)


class MissionStatusInput(BaseModel):
    mission_id: int = Field(
        default=0, ge=0,
        description="Mission ID for details including the result; 0 = overview of recent missions.",
    )


class MissionStatusTool(BaseTool):
    name = "mission_status"
    description = (
        "Shows the status of cloud missions: overview (mission_id=0) or details "
        "including the COMPLETE result (mission_id=<ID>). ALWAYS fetch a mission "
        "result with its concrete ID and take it over verbatim -- NEVER "
        "reconstruct it from memory or from the overview."
    )
    classification = ToolClassification.READ
    timeout_seconds = 15

    def get_input_schema(self) -> type[BaseModel]:
        return MissionStatusInput

    async def _execute(self, params: MissionStatusInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import get_result_text, list_missions_brief, wrap_untrusted_cloud_response
        if params.mission_id:
            rows = await asyncio.to_thread(list_missions_brief, 50)
            row = next((r for r in rows if r["id"] == params.mission_id), None)
            if row is None:
                return ToolOutput(success=False, error=f"Mission {params.mission_id} nicht gefunden.")
            lines = [
                f"Mission {row['id']}: {row['status']} ({row['provider']}, {row['calls']} Calls, "
                f"{row['tokens_in']}/{row['tokens_out']} Tokens rein/raus)",
                f"Schritt: {row['step']}",
                f"Ziel: {row['goal']}",
            ]
            if row["error"]:
                lines.append(f"Fehler: {row['error']}")
            result = await asyncio.to_thread(get_result_text, params.mission_id)
            if result:
                lines.append("Ergebnis:\n" + wrap_untrusted_cloud_response(result))
            return ToolOutput(success=True, data="\n".join(lines))
        rows = await asyncio.to_thread(list_missions_brief, 8)
        if not rows:
            return ToolOutput(success=True, data="Keine Missionen vorhanden.")
        lines = [
            f"#{r['id']} [{r['status']}] ({r['provider']}, {r['calls']} Calls, "
            f"{r['tokens_in']}/{r['tokens_out']} Tok) {r['goal']}"
            # Affordanz gegen Halluzination: fertige Missionen zeigen den Weg
            # zum echten Ergebnistext.
            + (f" -> Ergebnis: mission_status({r['id']})" if r["status"] == "done" else "")
            for r in rows
        ]
        return ToolOutput(success=True, data="\n".join(lines))


class CancelMissionInput(BaseModel):
    mission_id: int = Field(..., ge=1, description="ID of the mission to cancel.")


class CancelMissionTool(BaseTool):
    name = "cancel_mission"
    description = "Cancels a running or queued cloud mission."
    classification = ToolClassification.WRITE
    timeout_seconds = 15

    def get_input_schema(self) -> type[BaseModel]:
        return CancelMissionInput

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        # Abbrechen ist gutartig -- keine Bestaetigung noetig.
        return False

    async def _execute(self, params: CancelMissionInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import request_cancel
        ok = await asyncio.to_thread(request_cancel, params.mission_id)
        if not ok:
            return ToolOutput(success=False, error=f"Mission {params.mission_id} nicht gefunden oder bereits beendet.")
        return ToolOutput(success=True, data=f"Mission {params.mission_id} wird abgebrochen.")


class CloudAskInput(BaseModel):
    question: str = Field(
        ..., min_length=5, max_length=4000,
        description="Subject-matter question for the cloud AI. NO personal data.",
    )
    context: str = Field(
        default="", max_length=12000,
        description="Optional task-relevant context (redaction runs automatically).",
    )
    provider: str = Field(
        default="", pattern="^(|gemini)$",
        description="Optional: 'gemini'. Empty = default.",
    )


class CloudAskTool(BaseTool):
    name = "cloud_ask"
    description = (
        "Asks ONE question to an external cloud AI (Gemini) -- for heavy "
        "single tasks (reasoning, analysis, text) that overwhelm the local "
        "model. Gemini answers grounded via Google web search with source URLs; "
        "Claude has no web access. Question and context are pseudonymized "
        "beforehand (PII redaction + tripwire). For multi-source research "
        "prefer web_search/deep_research (local); for multi-step jobs use "
        "start_mission."
    )
    classification = ToolClassification.WRITE
    timeout_seconds = 180

    def get_input_schema(self) -> type[BaseModel]:
        return CloudAskInput

    def _requires_confirmation(self, detected: ToolClassification) -> bool:
        return not _auto_approve()

    def _impact_description(self, params: BaseModel) -> str:
        from rag_backend.cloud_agents import redactor
        red_q, mapping = redactor.redact_deterministic(params.question)
        red_ctx, mapping = redactor.redact_deterministic(params.context or "", mapping)
        preview = red_q + (("\n" + red_ctx) if red_ctx else "")
        if len(preview) > 900:
            preview = preview[:900] + " [...]"
        return (
            "Cloud-Anfrage stellen -- dieses redigierte Paket wird an eine externe "
            "KI (Gemini) übertragen:\n\n" + preview +
            "\n\n(Regex-Vorschau. Vor dem Versand läuft zusätzlich der Qwen-NER-Pass, "
            "der auch unbekannte Namen erkennt, sowie der PII-Tripwire.)"
        )

    async def _execute(self, params: CloudAskInput) -> ToolOutput:
        from rag_backend.cloud_agents.missions import run_cloud_ask
        from rag_backend.cloud_agents.providers import (
            CloudCallFailed,
            CloudNotConfigured,
            CloudRateLimited,
            any_configured,
        )
        from rag_backend.cloud_agents.redactor import RedactionUnavailable
        if os.getenv("CLOUD_AGENTS_ENABLED", "true").lower() != "true":
            return ToolOutput(success=False, error="Cloud-Agenten sind deaktiviert (CLOUD_AGENTS_ENABLED=false).")
        if not any_configured():
            return ToolOutput(
                success=False,
                error="Kein Cloud-Provider konfiguriert -- API-Key in API_Tokens/Gemini.txt hinterlegen.",
            )
        try:
            answer = await run_cloud_ask(params.question, params.context, params.provider)
            if isinstance(answer, str) and answer.startswith("Websuche und andere Zugriffe nach aussen"):
                return ToolOutput(success=False, error=answer)
            return ToolOutput(success=True, data=answer)
        except (CloudNotConfigured, CloudRateLimited, CloudCallFailed, RedactionUnavailable) as e:
            return ToolOutput(success=False, error=str(e))
