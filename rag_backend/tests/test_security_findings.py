"""Tests fuer die Sicherheitsfunde A bis F im Backend."""
import asyncio
import importlib
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pytest
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rag_backend import agent, crypto_utils, egress_guard, main as backend_main
from rag_backend.action_engine.tools.cloud_missions import CloudAskInput, CloudAskTool
from rag_backend.cloud_agents import missions, providers as providers_mod, redactor
from rag_backend.mcp_servers import filesystem as fs_mcp
from rag_backend.memory import EpisodicMemory
from rag_backend.models import ChatSession, Message, ReflectionState, User
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    TestingSessionLocal,
    _make_account,
    app as main_test_app,
    get_db as main_test_get_db,
    override_get_db,
    test_client,
)
from rag_backend.utils import cloud_enabled


# ==============================================================================
# A. Cloud-Schalter schliesst alle Wege
# ==============================================================================


def test_cloud_enabled_central_switch(monkeypatch):
    monkeypatch.delenv("CLOUD_AGENTS_ENABLED", raising=False)
    assert cloud_enabled() is False

    for falsy in ("", "false", "FALSE", "0", "1", "yes", "true_no", " on "):
        monkeypatch.setenv("CLOUD_AGENTS_ENABLED", falsy)
        assert cloud_enabled() is False, falsy

    for truthy in ("true", "TRUE", "  true  ", "True\n"):
        monkeypatch.setenv("CLOUD_AGENTS_ENABLED", truthy)
        assert cloud_enabled() is True, truthy


@pytest.mark.asyncio
async def test_launch_mission_and_cloud_ask_default_cloud_disabled(monkeypatch):
    monkeypatch.delenv("CLOUD_AGENTS_ENABLED", raising=False)
    egress_guard.reset_egress_state()

    ok, msg = await missions.launch_mission("Analysiere Markttrends 2026", "")
    assert ok is False
    assert "CLOUD_AGENTS_ENABLED=false" in msg

    out = await CloudAskTool()._execute(CloudAskInput(question="Was ist neu?"))
    assert out.success is False
    assert "CLOUD_AGENTS_ENABLED=false" in (out.error or "")


@pytest.mark.asyncio
async def test_cloud_collab_gates_cloud_enabled_auto_approve_and_role(monkeypatch):
    egress_guard.reset_egress_state()
    agent._collab_day_calls.clear()

    provider_calls = 0
    redactor_calls = 0

    class _CountingProvider:
        name = "gemini"
        model = "gemini-3.1-flash-lite"

        def is_configured(self):
            return True

        async def generate(self, system, prompt, max_tokens, grounding=False, model=None):
            nonlocal provider_calls
            provider_calls += 1
            return "ACCEPT", 10, 5

    async def _counting_redact(texts, existing_map=None):
        nonlocal redactor_calls
        redactor_calls += 1
        return list(texts), dict(existing_map or {})

    monkeypatch.setattr(providers_mod, "get_provider", lambda preferred=None: _CountingProvider())
    monkeypatch.setattr(redactor, "redact_outbound", _counting_redact)
    monkeypatch.setattr(redactor, "tripwire", lambda text: None)
    monkeypatch.setenv("CLOUD_COLLAB", "true")

    # 1. Cloud aus (Standard), trotz Auto-Approve und Admin -> kein Call, keine Redaction
    monkeypatch.delenv("CLOUD_AGENTS_ENABLED", raising=False)
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    agent.request_origin_var.set({"user_role": "admin"})
    assert await agent._cloud_collab("sys", "prompt") is None
    assert provider_calls == 0 and redactor_calls == 0

    # 2. Cloud an, aber Auto-Approve fehlt -> kein Call, keine Redaction
    monkeypatch.setenv("CLOUD_AGENTS_ENABLED", "true")
    monkeypatch.delenv("CLOUD_MISSION_AUTO_APPROVE", raising=False)
    agent.request_origin_var.set({"user_role": "admin"})
    assert await agent._cloud_collab("sys", "prompt") is None
    assert provider_calls == 0 and redactor_calls == 0

    # 3. Cloud an + Auto-Approve an, aber Rolle 'user' -> kein Call, keine Redaction
    monkeypatch.setenv("CLOUD_AGENTS_ENABLED", "true")
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    agent.request_origin_var.set({"user_role": "user"})
    assert await agent._cloud_collab("sys", "prompt") is None
    assert provider_calls == 0 and redactor_calls == 0

    # 4. Cloud an + Auto-Approve an + Rolle 'admin' -> Provider wird aufgerufen
    agent.request_origin_var.set({"user_role": "admin"})
    res = await agent._cloud_collab("sys", "prompt")
    assert res == "ACCEPT"
    assert provider_calls == 1 and redactor_calls == 1


@pytest.mark.asyncio
async def test_research_node_gates_cloud_enabled_auto_approve_and_role(monkeypatch):
    egress_guard.reset_egress_state()

    provider_calls = 0
    redactor_calls = 0

    class _CountingProvider:
        name = "gemini"
        model = "gemini-3.1-flash-lite"

        def is_configured(self):
            return True

        async def generate(self, system, prompt, max_tokens, grounding=False, model=None):
            nonlocal provider_calls
            provider_calls += 1
            return "Cloud-Hilfe zur Fehlerbehebung", 12, 8

    async def _counting_redact(texts, existing_map=None):
        nonlocal redactor_calls
        redactor_calls += 1
        return list(texts), dict(existing_map or {})

    monkeypatch.setattr(providers_mod, "get_provider", lambda preferred=None: _CountingProvider())
    monkeypatch.setattr(redactor, "redact_outbound", _counting_redact)
    monkeypatch.setattr(redactor, "tripwire", lambda text: None)

    state = {
        "blackboard": {
            "task": "Fixe das Skript",
            "code_draft": "print(1/0)",
            "issue_description": "ZeroDivisionError",
            "language": "python",
        }
    }

    # 1. Cloud aus -> Fallback, kein Provider-Call, keine Redaction
    monkeypatch.delenv("CLOUD_AGENTS_ENABLED", raising=False)
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    agent.request_origin_var.set({"user_role": "admin"})
    out1 = await agent.research_node(state)
    assert "No cloud research available" in out1["blackboard"]["research_data"]
    assert provider_calls == 0 and redactor_calls == 0

    # 2. Cloud an, aber Auto-Approve aus -> Fallback, kein Provider-Call, keine Redaction
    monkeypatch.setenv("CLOUD_AGENTS_ENABLED", "true")
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "false")
    agent.request_origin_var.set({"user_role": "admin"})
    out2 = await agent.research_node(state)
    assert "No cloud research available" in out2["blackboard"]["research_data"]
    assert provider_calls == 0 and redactor_calls == 0

    # 3. Cloud an + Auto-Approve an, aber Rolle 'user' -> Fallback, kein Provider-Call
    monkeypatch.setenv("CLOUD_AGENTS_ENABLED", "true")
    monkeypatch.setenv("CLOUD_MISSION_AUTO_APPROVE", "true")
    agent.request_origin_var.set({"user_role": "user"})
    out3 = await agent.research_node(state)
    assert "No cloud research available" in out3["blackboard"]["research_data"]
    assert provider_calls == 0 and redactor_calls == 0

    # 4. Cloud an + Auto-Approve an + Rolle 'admin' -> Provider wird aufgerufen
    agent.request_origin_var.set({"user_role": "admin"})
    out4 = await agent.research_node(state)
    assert out4["blackboard"]["research_data"] == "Cloud-Hilfe zur Fehlerbehebung"
    assert provider_calls == 1 and redactor_calls == 1


@pytest.mark.asyncio
async def test_provider_logs_only_key_index_not_key_prefix(caplog):
    secret_key = "AIzaSyGeheimerSchluessel987654321"

    class _FailingProvider(providers_mod.CloudProvider):
        name = "testprov"
        key_var = "GEMINI_API_KEY"

        def _api_keys(self):
            return [secret_key]

        async def _generate(self, system, prompt, max_tokens, grounding=False, model=None):
            raise RuntimeError("kaputt")

    prov = _FailingProvider(rpm=10, rpd=100)
    with caplog.at_level("WARNING"):
        with pytest.raises(providers_mod.CloudCallFailed):
            await prov.generate("sys", "prompt", 100)

    assert "Key 0" in caplog.text
    assert secret_key[:10] not in caplog.text
    assert secret_key not in caplog.text


# ==============================================================================
# B. Gedaechtnisdateien und Unterbaum users/
# ==============================================================================


def test_fs_tools_block_entire_users_subtree(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_mcp, "WORKSPACE_CONTAINER", tmp_path)

    # Dateien im Workspace und unter users/ anlegen
    (tmp_path / "Uploads").mkdir()
    (tmp_path / "Uploads" / "offen.txt").write_text("erlaubt", encoding="utf-8")
    u5 = tmp_path / "users" / "5"
    (u5 / "memory").mkdir(parents=True)
    (u5 / "USER.md").write_text("Profil", encoding="utf-8")
    (u5 / "MEMORY.md").write_text("Gedaechtnis", encoding="utf-8")
    (u5 / "memory" / "2026-11-16.md").write_text("Tagesnotiz", encoding="utf-8")

    forbidden_paths = [
        "users",
        "users/",
        "Users",
        "Users/",
        "USERS/5/MEMORY.md",
        "users/5/MEMORY.md",
        "users/5/USER.md",
        "users/5/memory/2026-11-16.md",
        "users\\5\\MEMORY.md",
        "Users\\5\\MEMORY.md",
        "Uploads/../users/5/MEMORY.md",
        "Uploads\\..\\Users\\5\\MEMORY.md",
        r"C:\Argus_Workspace\users",
        r"C:\Argus_Workspace\Users",
        r"C:\Argus_Workspace\users\5\MEMORY.md",
        r"C:\Argus_Workspace\Users\5\MEMORY.md",
        str(u5 / "MEMORY.md"),
        str(tmp_path / "users"),
    ]

    for p in forbidden_paths:
        with pytest.raises(PermissionError, match="isolated user"):
            fs_mcp.fs_read_file(p)
        with pytest.raises(PermissionError, match="isolated user"):
            fs_mcp.fs_write_file(p, "manipuliert")
        with pytest.raises(PermissionError, match="isolated user"):
            fs_mcp.fs_list_directory(p)
        with pytest.raises(PermissionError, match="isolated user"):
            fs_mcp.fs_mkdir(p)

    # Auflistung des Arbeitsordners: 'users' darf weder flach noch rekursiv erscheinen
    listing_flat = fs_mcp.fs_list_directory(r"C:\Argus_Workspace", recursive=False)
    assert "Uploads" in listing_flat
    assert "users" not in listing_flat.lower()

    listing_rec = fs_mcp.fs_list_directory(r"C:\Argus_Workspace", recursive=True)
    assert "offen.txt" in listing_rec
    assert "users" not in listing_rec.lower()
    assert "memory.md" not in listing_rec.lower()

    # Symlink-Pruefung (falls vom Dateisystem unterstuetzt)
    link_path = tmp_path / "Uploads" / "link_mem.md"
    try:
        os.symlink(str(u5 / "MEMORY.md"), str(link_path))
        has_symlink = True
    except (OSError, NotImplementedError):
        has_symlink = False

    if has_symlink:
        with pytest.raises(PermissionError):
            fs_mcp.fs_read_file("Uploads/link_mem.md")
        with pytest.raises(PermissionError):
            fs_mcp.fs_write_file("Uploads/link_mem.md", "ueberschrieben")


@pytest.mark.asyncio
async def test_reflection_skips_sensitive_sessions_and_advances_watermark(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr("rag_backend.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))

    t1 = datetime(2026, 11, 16, 10, 0, 0)
    t2 = datetime(2026, 11, 16, 10, 5, 0)
    t3 = datetime(2026, 11, 16, 10, 10, 0)

    with TestingSessionLocal() as db:
        u = User(id=77, email="ref_user", hashed_password="x", role="user")
        db.add(u)
        db.commit()

        s_normal = ChatSession(user_id=77, has_sensitive_data=False)
        s_sens = ChatSession(user_id=77, has_sensitive_data=True)
        db.add_all([s_normal, s_sens])
        db.commit()
        db.refresh(s_normal)
        db.refresh(s_sens)
        sid_normal = s_normal.id
        sid_sens = s_sens.id

        db.add_all([
            Message(
                session_id=sid_normal,
                sender="user",
                content_encrypted=crypto_utils.encrypt_msg("Ich trinke gerne gruenen Tee."),
                timestamp=t1,
            ),
            Message(
                session_id=sid_sens,
                sender="user",
                content_encrypted=crypto_utils.encrypt_msg("Geheimer Übernahmepreis 42 Mio EUR."),
                timestamp=t2,
            ),
        ])
        db.commit()

    mem = EpisodicMemory()
    mem._ws = tmp_path

    # 1. _load_new_messages filtert das sensible Gespraech aus, nimmt aber max_ts=t2 mit
    rows, max_ts = mem._load_new_messages(77)
    assert rows == [("user", "Ich trinke gerne gruenen Tee.")]
    assert max_ts == t2

    seen_prompts = []

    async def _fake_ainvoke(messages, **_k):
        seen_prompts.append(messages[-1].content)
        return AIMessage(content="- User likes green tea.")

    fake_llm = MagicMock()
    fake_llm.ainvoke = _fake_ainvoke
    monkeypatch.setattr(agent, "_llm_fast", fake_llm)

    wrote = await mem.reflect(77)
    assert wrote == 1
    assert len(seen_prompts) == 1
    assert "gruenen Tee" in seen_prompts[0]
    assert "Übernahmepreis" not in seen_prompts[0]

    with TestingSessionLocal() as db:
        st = db.get(ReflectionState, 77)
        assert st is not None and st.reflected_until == t2

    # 2. Neue Nachricht NUR im sensiblen Gespraech: kein LLM-Aufruf, aber Wasserzeichen laeuft auf t3
    with TestingSessionLocal() as db:
        db.add(Message(
            session_id=sid_sens,
            sender="assistant",
            content_encrypted=crypto_utils.encrypt_msg("Weitere geheime Firmendaten."),
            timestamp=t3,
        ))
        db.commit()

    wrote2 = await mem.reflect(77)
    assert wrote2 == 0
    assert len(seen_prompts) == 1  # kein weiterer LLM-Aufruf

    with TestingSessionLocal() as db:
        st2 = db.get(ReflectionState, 77)
        assert st2 is not None and st2.reflected_until == t3

    # 3. Naechster Lauf sieht keine unverarbeiteten Nachrichten mehr
    rows_after, max_ts_after = mem._load_new_messages(77)
    assert rows_after == [] and max_ts_after is None


# ==============================================================================
# C. Standardwerte und Pruefung fremder Inhalte (WEB_INJECTION_GUARD)
# ==============================================================================


@pytest.mark.asyncio
async def test_web_injection_guard_default_on_and_fail_closed(monkeypatch):
    egress_guard.reset_egress_state()
    monkeypatch.delenv("WEB_INJECTION_GUARD", raising=False)
    assert agent._web_injection_guard_enabled() is True

    async def _fake_fetch(url: str) -> str:
        return "Sauberer Seiteninhalt mit genug Zeichen fuer den Test."

    monkeypatch.setattr(agent, "_fetch_clean_page", _fake_fetch)

    # 1. Ausnahme im Pruefer -> Inhalt wird NICHT uebernommen, ehrliche Meldung
    async def _raising_ainvoke(messages, **_k):
        raise RuntimeError("LLM-Server nicht erreichbar")

    fake_llm = MagicMock()
    fake_llm.ainvoke = _raising_ainvoke
    monkeypatch.setattr(agent, "llm_task", fake_llm)

    flagged, reason = await agent._judge_web_injection("Testinhalt")
    assert flagged is True
    assert "Pruefung nicht moeglich" in reason

    page_res = await agent.read_webpage.ainvoke({"url": "https://example.org/test"})
    assert page_res.startswith("WARN_INJECTION:")
    assert "not possible" in page_res
    assert "NOT ingested" in page_res
    assert "manipulation" not in page_res.lower()
    assert "Sauberer Seiteninhalt" not in page_res

    # 2. Unlesbare Antwort: einmal wiederholen; wenn beide Male kein JSON -> nicht uebernehmen
    calls = []

    async def _bad_json_twice(messages, **_k):
        calls.append(1)
        return AIMessage(content="Keine JSON-Antwort vom Modell")

    fake_llm.ainvoke = _bad_json_twice
    flagged2, reason2 = await agent._judge_web_injection("Ein einzelner Chunk")
    assert flagged2 is True
    assert len(calls) == 2
    assert "Pruefung nicht moeglich" in reason2

    # 3. Erster Versuch kein JSON, zweiter Versuch gueltiges JSON -> wird akzeptiert
    calls.clear()

    async def _bad_then_good(messages, **_k):
        calls.append(1)
        if len(calls) == 1:
            return AIMessage(content="kaputt")
        return AIMessage(content='{"injection_detected": false, "reason": ""}')

    fake_llm.ainvoke = _bad_then_good
    flagged3, reason3 = await agent._judge_web_injection("Ein einzelner Chunk")
    assert flagged3 is False and reason3 == ""
    assert len(calls) == 2


# ==============================================================================
# D. Sitzungs-Cookie darf nicht als API-Bearer-Token gelten
# ==============================================================================


def test_dashboard_cookie_token_rejected_as_bearer_token(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    cookie_token = client.cookies.get("argus_session")
    assert cookie_token

    # Als Cookie am Dashboard gueltig
    assert client.get("/v1/dashboard/chats").status_code == 200

    # Als Bearer-Token an API-Routen (get_current_user) mit 401 abgewiesen
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {cookie_token}"}
    assert client.get("/v1/missions", headers=headers).status_code == 401
    assert client.get("/v1/confirmations/pending", headers=headers).status_code == 401

    # Echtes API-Token (ohne scope == "dashboard") funktioniert weiterhin
    api_token = backend_main.create_token(1, 0)
    api_headers = {"Authorization": f"Bearer {api_token}"}
    assert client.get("/v1/missions", headers=api_headers).status_code == 200
    assert client.get("/v1/confirmations/pending", headers=api_headers).status_code == 200


# ==============================================================================
# E. LangSmith-Schluessel und Tracing-Schalter
# ==============================================================================


def test_langsmith_key_not_loaded_and_tracing_forced_false(monkeypatch):
    requested_secrets = []
    orig_load_secret = crypto_utils.load_secret

    def _spy_load_secret(name: str) -> str:
        requested_secrets.append(name)
        if name == "LANGCHAIN_API_KEY":
            return "lsv2_should_never_be_loaded"
        return orig_load_secret(name)

    monkeypatch.setattr(crypto_utils, "load_secret", _spy_load_secret)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_preexisting")

    importlib.reload(agent)

    assert "LANGCHAIN_API_KEY" not in requested_secrets
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"
    assert os.environ.get("LANGSMITH_TRACING") == "false"
    assert os.environ.get("LANGCHAIN_TRACING") == "false"


# ==============================================================================
# F. Sandbox-Container darf das Backend nicht erreichen
# ==============================================================================


def test_sandbox_middleware_blocks_sandbox_ip_and_allows_others(db_session, monkeypatch):
    #--- Aussen nach innen: Sandbox-Sperre (echte Gegenstelle), Proxy-Header, Host-Pruefung.
    #--- Vergleich ueber Namen: test_company_profile laedt rag_backend.main neu.
    outer = main_test_app.user_middleware
    assert outer[0].cls is BaseHTTPMiddleware
    assert outer[0].kwargs["dispatch"].__name__ == "block_sandbox_requests"
    assert outer[1].cls.__name__ == "_TrustedProxyHeaders"
    assert outer[2].cls is TrustedHostMiddleware

    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset({"172.19.0.55"}))
    main_test_app.dependency_overrides[main_test_get_db] = lambda: db_session
    try:
        # 1. Anfrage von der Sandbox-IP -> 403
        with TestClient(
            main_test_app,
            base_url="http://127.0.0.1:7860",
            client=("172.19.0.55", 50000),
        ) as sandbox_client:
            assert sandbox_client.get("/healthz").status_code == 403
            assert sandbox_client.get("/v1/dashboard/auth/state").status_code == 403

        # 2. Anfrage von einer anderen Adresse -> 200
        with TestClient(
            main_test_app,
            base_url="http://127.0.0.1:7860",
            client=("172.19.0.10", 50000),
        ) as other_client:
            assert other_client.get("/healthz").status_code == 200
            assert other_client.get("/v1/dashboard/auth/state").status_code == 200

        # 3. /healthz aus dem eigenen Container (127.0.0.1) geht immer
        with TestClient(
            main_test_app,
            base_url="http://127.0.0.1:7860",
            client=("127.0.0.1", 50000),
        ) as local_client:
            assert local_client.get("/healthz").status_code == 200
    finally:
        main_test_app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sandbox_ip_watcher_and_resolve(monkeypatch):
    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset())

    # 1. _resolve_sandbox_ips mit gemocktem socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.19.0.77", 0))]
        if host == "calc-sandbox"
        else [],
    )
    assert backend_main._resolve_sandbox_ips() == frozenset({"172.19.0.77"})

    # 2. Hintergrundaufgabe _sandbox_ip_watcher mit ersetzter _resolve_sandbox_ips
    calls = 0

    def _fake_resolve():
        nonlocal calls
        calls += 1
        if calls == 1:
            return frozenset({"172.19.0.88"})
        raise socket.gaierror("DNS voruebergehend nicht erreichbar")

    async def _fast_sleep(_seconds):
        if calls >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(backend_main, "_resolve_sandbox_ips", _fake_resolve)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await backend_main._sandbox_ip_watcher()

    # Nach erstem Erfolg gesetzt und beim zweiten Fehlschlag beibehalten
    assert calls == 2
    assert backend_main._SANDBOX_IPS == frozenset({"172.19.0.88"})
    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset())
