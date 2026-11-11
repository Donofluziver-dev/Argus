"""Werkzeugauswahl und Recherche-Deckel des Agenten (Befunde der Agenten-Messung)."""
import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("PII_BLOCKLIST_PATH", "/nonexistent/pii_blocklist.txt")

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from rag_backend import agent


def _names(ts):
    return {getattr(t, "name", "") for t in ts}


def _fake_tool(name, calls):
    async def run(query: str) -> str:
        calls.append(query)
        return f"Bericht zu {query}"
    return StructuredTool.from_function(coroutine=run, name=name, description=f"Testwerkzeug {name}")


def test_document_search_is_always_offered():
    # Auch wenn der Router nur 'web' oder gar keine Gruppe waehlt.
    for groups in ([], ["web"], ["code"]):
        picked = _names(agent._tools_for_groups(groups))
        assert "document_search" in picked, groups
    # Die Verwaltungswerkzeuge bleiben in der Gruppe.
    assert "index_document" not in _names(agent._tools_for_groups(["web"]))
    assert "index_document" in _names(agent._tools_for_groups(["docs"]))


def test_normal_account_keeps_document_search_but_not_admin_tools():
    offered = _names(agent._tools_for_role(list(agent.tools), "user"))
    assert "document_search" in offered
    assert not offered & agent._ADMIN_ONLY_TOOLS
    assert not any(n.startswith("fs_") for n in offered)


def test_internal_knowledge_rule_names_document_search():
    rule = agent._INTERNAL_KNOWLEDGE_RULE
    assert "Call document_search BEFORE" in rule
    assert "Never fill in a figure" in rule


def _ai_calls(*queries, name="deep_research", start=0):
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": {"query": q}, "id": f"c{start + i}"} for i, q in enumerate(queries)])


def test_research_cap_per_question(monkeypatch):
    monkeypatch.setenv("RESEARCH_CALLS_PER_TURN", "2")
    calls: list = []
    node = agent.make_tool_node([_fake_tool("deep_research", calls)])
    state = {"messages": [HumanMessage(content="Frage"), _ai_calls("a", "b", "c")]}
    out = asyncio.run(node(state))
    assert calls == ["a", "b"]
    assert out["messages"][2].content.startswith("Research limit for this question reached")


def test_research_cap_counts_earlier_calls_of_same_question_only(monkeypatch):
    monkeypatch.setenv("RESEARCH_CALLS_PER_TURN", "2")
    calls: list = []
    node = agent.make_tool_node([_fake_tool("deep_research", calls)])
    earlier_turn = [HumanMessage(content="alte Frage"), _ai_calls("x", "y"),
                    ToolMessage(content="r", tool_call_id="c0", name="deep_research"),
                    ToolMessage(content="r", tool_call_id="c1", name="deep_research")]
    this_turn = [HumanMessage(content="neue Frage"), _ai_calls("a", start=10),
                 ToolMessage(content="r", tool_call_id="c10", name="deep_research"),
                 _ai_calls("b", "c", start=20)]
    asyncio.run(node({"messages": earlier_turn + this_turn}))
    assert calls == ["b"]


class _LoopingLLM:
    """Ruft in jedem Schritt zwei neue Websuchen auf, ohne je zu berichten."""

    def __init__(self):
        self.step = 0

    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, messages, **_k):
        if messages and "STOP" in str(getattr(messages[-1], "content", "")):
            return AIMessage(content="Bericht aus dem Gelesenen")
        self.step += 1
        return _ai_calls(f"s{self.step}a", f"s{self.step}b", name="web_search", start=self.step * 10)


def test_subagent_search_budget(monkeypatch):
    monkeypatch.setenv("SUBAGENT_MAX_STEPS", "6")
    monkeypatch.setenv("SUBAGENT_MAX_SEARCHES", "3")
    llm = _LoopingLLM()
    monkeypatch.setattr(agent, "_llm_fast", llm)
    monkeypatch.setattr(agent, "_serving_budget", lambda: 100_000)
    monkeypatch.setattr(agent, "_dynamic_max_tokens", lambda *a, **k: 512)

    async def no_compaction(msgs, *_a, **_k):
        return msgs
    monkeypatch.setattr(agent, "_compact_messages_if_overflow", no_compaction)

    searches: list = []
    report = asyncio.run(agent.run_subagent_loop(
        role="Test", task="Recherche", think=False,
        allowed_tools=[_fake_tool("web_search", searches)]))
    assert len(searches) == 3
    assert report.startswith("Bericht aus dem Gelesenen")
