'''Cloud-Agenten (Setup4): Delegation an Gemini mit PII-Schutz.

Das lokale Qwen bleibt Orchestrator und einzige Instanz mit Rohdaten-Zugriff.
Alles, was den Host verlaesst, laeuft durch redactor.py (Pseudonymisierung +
Tripwire). Missionen arbeiten im Hintergrund-Loop (missions.mission_loop).
'''


def register_cloud_tools() -> list:
    # Liefert die LangChain-Tools der Cloud-Agenten.
    from rag_backend.action_engine.tools.cloud_missions import (
        StartMissionTool,
        MissionStatusTool,
        CancelMissionTool,
        CloudAskTool,
    )
    return [
        StartMissionTool().to_langchain_tool(),
        MissionStatusTool().to_langchain_tool(),
        CancelMissionTool().to_langchain_tool(),
        CloudAskTool().to_langchain_tool(),
    ]
