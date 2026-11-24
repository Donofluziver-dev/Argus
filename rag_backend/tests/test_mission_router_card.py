"""Erkennt der Router eine Mission, startet sie nur nach Freigabe auf der Karte im Chat.

Vorher rief agent_node bei task_type == "mission" direkt launch_mission auf und umging
damit die Freigabekarte, die das Werkzeug start_mission zeigt.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("CLOUD_REDACTION_LLM_PASS", "false")

import pytest
from langchain_core.messages import HumanMessage

from rag_backend import agent, egress_guard
from rag_backend.action_engine.confirmation import ConfirmationStatus, confirmation_store
from rag_backend.action_engine.tools.cloud_missions import _auto_approve
from rag_backend.cloud_agents import missions

GOAL = "Starte eine Mission: Marktanalyse der Cloud-Anbieter 2026 mit Quellen"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    egress_guard.reset_egress_state()
    monkeypatch.delenv("CLOUD_MISSION_AUTO_APPROVE", raising=False)
    monkeypatch.setenv("CLOUD_REDACTION_LLM_PASS", "false")
    monkeypatch.setattr("rag_backend.crypto_utils.load_secret", lambda name: "")
    #--- Modul an, Anbieter vorhanden: sonst gaebe es zu Recht gar keine Karte.
    monkeypatch.setattr(missions, "mission_start_unavailable", lambda: None)
    token = agent.request_origin_var.set(
        {"user_role": "admin", "channel": "dashboard", "task_id": "t-mission"})
    yield
    agent.request_origin_var.reset(token)
    egress_guard.reset_egress_state()


@pytest.fixture
def router_says_mission(monkeypatch):
    async def _classify(_text):
        return {"task_type": "mission", "needs_reasoning": False,
                "needs_web_search": False, "use_blackboard_loop": False}
    monkeypatch.setattr(agent, "_classify_routing_decision", _classify)


@pytest.fixture
def launches(monkeypatch):
    calls = []

    async def _launch(goal, context, provider=""):
        calls.append({"goal": goal, "context": context})
        return True, "Mission 42 gestartet."

    monkeypatch.setattr(missions, "launch_mission", _launch)
    return calls


@pytest.fixture
def cards(monkeypatch):
    created = []
    original = confirmation_store.create

    def _create(**kwargs):
        req = original(**kwargs)
        created.append(req)
        return req

    monkeypatch.setattr(confirmation_store, "create", _create)
    return created


def _answer_card(monkeypatch, status):
    async def _wait(_cid):
        return SimpleNamespace(status=status)
    monkeypatch.setattr(confirmation_store, "wait_for_resolution", _wait)


def _state(text, user_id=1):
    return {"messages": [HumanMessage(content=text)], "user_id": user_id,
            "seen_tool_hashes": set(), "history_summary": "", "blackboard": {},
            "iteration": 0, "use_reasoning": False, "task_type": "reasoning",
            "tool_groups": [], "tool_error_count": 0, "tool_error_history": [],
            "cloud_consulted": False}


@pytest.mark.asyncio
async def test_rejected_card_starts_nothing(monkeypatch, router_says_mission, launches, cards):
    _answer_card(monkeypatch, ConfirmationStatus.REJECTED)
    result = await agent.agent_node(_state(GOAL))
    assert launches == []
    assert len(cards) == 1 and cards[0].tool_name == "start_mission"
    assert "Gemini" in cards[0].impact_description          # dieselbe Vorschau wie beim Werkzeug
    assert result.get("task_type") != "mission"
    assert "abgelehnt" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_approved_card_starts_mission(monkeypatch, router_says_mission, launches, cards):
    _answer_card(monkeypatch, ConfirmationStatus.APPROVED)
    result = await agent.agent_node(_state(GOAL))
    assert len(cards) == 1
    assert [c["goal"] for c in launches] == [GOAL]
    assert result["task_type"] == "mission"
    assert result["messages"][-1].content == "Mission 42 gestartet."


@pytest.mark.asyncio
async def test_auto_approve_starts_directly(monkeypatch, router_says_mission, launches, cards):
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    result = await agent.agent_node(_state(GOAL))
    assert cards == []
    assert len(launches) == 1
    assert result["task_type"] == "mission"


@pytest.mark.asyncio
async def test_company_data_always_needs_card(monkeypatch, router_says_mission, launches, cards):
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    monkeypatch.setenv("EGRESS_AFTER_SENSITIVE", "allow")
    egress_guard.mark_sensitive_source("document_search", "Internes Strategiepapier 2026.")
    assert _auto_approve() is False
    _answer_card(monkeypatch, ConfirmationStatus.REJECTED)
    result = await agent.agent_node(_state(GOAL))
    assert launches == []
    assert len(cards) == 1
    assert result.get("task_type") != "mission"


@pytest.mark.asyncio
async def test_short_goal_gives_message_without_card(monkeypatch, router_says_mission, launches, cards):
    result = await agent.agent_node(_state("Mission!"))
    assert cards == [] and launches == []
    assert "zu kurz" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_long_goal_keeps_everything(monkeypatch, router_says_mission, launches, cards):
    _answer_card(monkeypatch, ConfirmationStatus.APPROVED)
    text = "Analysiere bitte " + "x" * 4990 + " ENDE"
    result = await agent.agent_node(_state(text))
    assert result["task_type"] == "mission"
    assert len(launches[0]["goal"]) == 4000
    assert launches[0]["context"].endswith(" ENDE")          # Rest steht vorn im Kontext


@pytest.mark.asyncio
async def test_no_card_when_mission_cannot_run(monkeypatch, router_says_mission, launches, cards):
    monkeypatch.setattr(missions, "mission_start_unavailable",
                        lambda: "Cloud-Agenten sind deaktiviert (CLOUD_AGENTS_ENABLED=false).")
    result = await agent.agent_node(_state(GOAL))
    assert cards == [] and launches == []
    assert "deaktiviert" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_user_role_never_reaches_mission(monkeypatch, router_says_mission, launches, cards):
    agent.request_origin_var.set({"user_role": "user", "channel": "dashboard"})
    seen = []

    async def _router_mission(*args):
        seen.append(args)
        return {}
    monkeypatch.setattr(agent, "_router_mission", _router_mission)

    class _Weiter(Exception):
        pass

    def _stop():
        raise _Weiter()
    #--- Der erste Aufruf nach dem Missionszweig: dort endet der Test.
    monkeypatch.setattr("rag_backend.features.is_web_available", _stop)
    with pytest.raises(_Weiter):
        await agent.agent_node(_state(GOAL))
    assert seen == [] and cards == [] and launches == []


@pytest.mark.asyncio
async def test_card_reaches_the_chat_stream(monkeypatch, router_says_mission, launches):
    """Die offene Freigabe muss als Ereignis aus dem Graphen kommen; daraus baut der
    Dashboard-Chat die Karte (on_custom_event 'argus_confirmation' -> 'confirmation')."""
    from langgraph.graph import END, START, StateGraph

    _answer_card(monkeypatch, ConfirmationStatus.REJECTED)
    graph = StateGraph(agent.State)
    graph.add_node("agent", agent.agent_node)
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    events = [e async for e in graph.compile().astream_events(_state(GOAL), version="v2")]
    phases = [e["data"].get("phase") for e in events
              if e["event"] == "on_custom_event" and e["name"] == "argus_confirmation"]
    assert phases == ["pending", "resolved"]
    assert launches == []
