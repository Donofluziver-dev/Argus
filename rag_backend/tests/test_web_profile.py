"""Tests fuer Auftrag E: Internetzugang (Websuche und Seitenabruf) als abschaltbares Compose-Profil `web`."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pytest

from rag_backend import features
from rag_backend import main as backend
from rag_backend import metrics
from rag_backend import prompts
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_setup_module(name: str, filename: str):
    path = REPO_ROOT / "setup" / filename
    if not path.exists():
        pytest.skip(f"setup/{filename} nicht eingebunden")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# 1. features.py: Zustand, Watcher und DNS-Aufloesung von SearXNG
# =============================================================================


def test_features_default_state_and_availability():
    """Vor der ersten DNS-Pruefung gilt web (wie voice und tracing) als vorhanden."""
    assert features._OPTIONAL_SERVICES.get("web") is True
    assert features.is_web_available() is True
    assert backend.is_web_available() is True
    cur = features.current_features()
    assert cur["web"] is True


@pytest.mark.asyncio
async def test_features_check_optional_services_once_resolves_searxng(monkeypatch):
    seen: list[str] = []

    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        seen.append(host)
        if host == "searxng":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.8", 0))]
        raise socket.gaierror("host not found")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("SEARXNG_URL", "http://searxng:8080")

    state = await features._check_optional_services_once()
    assert state["web"] is True
    assert state["voice"] is False
    assert state["tracing"] is False
    assert "searxng" in seen

    # Ohne SearXNG-Aufloesung: web ist False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: (_ for _ in ()).throw(socket.gaierror("none")))
    state_no_web = await features._check_optional_services_once()
    assert state_no_web["web"] is False


@pytest.mark.asyncio
async def test_searxng_online_skips_http_when_web_disabled(monkeypatch):
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": False})

    called = []

    class ForbiddenClient:
        def __init__(self, *a, **kw):
            called.append(1)
            raise AssertionError("SearXNG-Healthcheck darf ohne Profil web nicht aufgerufen werden")

    monkeypatch.setattr(metrics.httpx, "AsyncClient", ForbiddenClient)
    res = await metrics.searxng_online()
    assert res is False
    assert called == []


# =============================================================================
# 2. agent.py: Werkzeuge ohne Internet nicht angeboten (Hauptagent und Sub-Agenten)
# =============================================================================


def test_agent_tools_filtered_when_web_disabled(monkeypatch):
    from rag_backend import agent

    # 1. Mit Web: Alle Web-Werkzeuge sind vorhanden
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": True})
    active_with_web = agent._tools_for_role(agent.tools, role="admin")
    names_with_web = {getattr(t, "name", "") for t in active_with_web}
    for w in ("web_search", "read_webpage", "deep_research", "get_weather", "route_distance"):
        assert w in names_with_web, f"Werkzeug {w} fehlt mit Web"

    # Router Flags mit Web erzwingen web
    assert "web" in (agent._effective_tool_groups({"needs_web_search": True}) or [])
    assert "web" in (agent._effective_tool_groups({"task_type": "research"}) or [])

    # 2. Ohne Web: Alle Web-Tools und get_weather sind entfernt
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": False})
    active_no_web = agent._tools_for_role(agent.tools, role="admin")
    names_no_web = {getattr(t, "name", "") for t in active_no_web}
    web_and_weather = agent._TOOL_GROUP_NAMES["web"] | {"get_weather"}
    for w in web_and_weather:
        assert w not in names_no_web, f"Werkzeug {w} darf ohne Web nicht angeboten werden"

    # Basiswerkzeuge bleiben erhalten
    for base in ("document_search", "calculate", "datetime_now"):
        assert base in names_no_web, f"Basis-Werkzeug {base} muss erhalten bleiben"

    # Auch fuer normale Nutzer (nicht admin) gefiltert
    user_no_web = agent._tools_for_role(agent.tools, role="user")
    user_names = {getattr(t, "name", "") for t in user_no_web}
    for w in web_and_weather:
        assert w not in user_names

    # Router darf needs_web nicht erzwingen, wenn Web aus ist
    eff_groups = agent._effective_tool_groups({"needs_web_search": True, "task_type": "research"})
    assert "web" not in (eff_groups or [])

    picked = agent._tools_for_groups(["web"], needs_web=True)
    picked_names = {getattr(t, "name", "") for t in picked}
    for w in web_and_weather:
        assert w not in picked_names


def test_research_subagent_toolset_filtered_without_web(monkeypatch):
    from rag_backend import agent

    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": False})

    # Testet die Werkzeugliste fuer den Wave-Worker
    toolset = [agent.web_search, agent.read_webpage, agent.deep_research, agent.calculate,
               agent.datetime_now, agent.get_weather, agent.route_distance]
    web_names = agent._TOOL_GROUP_NAMES["web"] | {"get_weather"}
    filtered = [t for t in toolset if getattr(t, "name", "") not in web_names]
    filtered_names = {getattr(t, "name", "") for t in filtered}
    assert filtered_names == {"calculate", "datetime_now"}


# =============================================================================
# 3. Prompts: Web-Abschnitte ohne Internet nicht beschrieben
# =============================================================================


def test_prompts_render_without_web_tools():
    identity_dir = REPO_ROOT / "rag_backend" / "prompts" / "identity"
    soul_raw = (identity_dir / "SOUL.md").read_text(encoding="utf-8")
    skill_raw = (identity_dir / "SKILL.md").read_text(encoding="utf-8")
    agents_raw = (identity_dir / "AGENTS.md").read_text(encoding="utf-8")

    # Mit Web: Abschnitte vorhanden
    tools_with_web = {"web_search", "read_webpage", "document_search", "calculate", "datetime_now"}
    soul_web = prompts.render(soul_raw, tool_names=tools_with_web)
    skill_web = prompts.render(skill_raw, tool_names=tools_with_web)
    agents_web = prompts.render(agents_raw, tool_names=tools_with_web)

    assert "web_search" in soul_web
    assert "<term_preservation>" in soul_web
    assert "read_webpage" in skill_web
    assert "- **research**" in agents_web

    # Ohne Web: keine Web-Werkzeuge beschrieben
    tools_no_web = {"document_search", "calculate", "datetime_now"}
    soul_no = prompts.render(soul_raw, tool_names=tools_no_web)
    skill_no = prompts.render(skill_raw, tool_names=tools_no_web)
    agents_no = prompts.render(agents_raw, tool_names=tools_no_web)

    assert "web_search" not in soul_no
    assert "<term_preservation>" not in soul_no
    assert "read_webpage" not in skill_no
    assert "route_distance" not in skill_no
    assert "- **research**" not in agents_no
    # Nicht-Web-Werkzeuge bleiben
    assert "document_search" in skill_no
    assert "calculate" in skill_no
    assert "- **parallel_agents**" in agents_no


# =============================================================================
# 4. /v1/dashboard/web_search: 503 ohne Internet ohne SearXNG anzufragen
# =============================================================================


def test_dashboard_web_search_503_without_web_and_no_backend_call(client, monkeypatch):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": False})

    called = []

    def forbidden_search(*a, **kw):
        called.append(1)
        raise AssertionError("SearXNG darf ohne Profil web nicht aufgerufen werden")

    from rag_backend import agent
    monkeypatch.setattr(agent, "_search_and_read", forbidden_search)

    res = client.post(
        "/v1/dashboard/web_search",
        headers=DASH_ORIGIN,
        json={"query": "Test-Anfrage"},
    )
    assert res.status_code == 503
    assert res.json()["detail"] == "Websuche ist auf diesem Server nicht eingerichtet."
    assert called == []


# =============================================================================
# 5. setup/init_stack.py: Migrationen mit COMPOSE_PROFILES und ARGUS_ENV_VERSION
# =============================================================================


def test_init_stack_migration_v2(tmp_path, monkeypatch):
    init_stack = _load_setup_module("init_stack", "init_stack.py")
    monkeypatch.setattr(init_stack, "restrict_to_owner", lambda _d: False)
    monkeypatch.setattr(init_stack.ensure_stack_secrets, "__defaults__", (lambda _d: False,))

    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    shutil.copy2(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    ws = tmp_path / "ws"
    models = tmp_path / "models"

    base_env = (
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n"
    )

    # 1. COMPOSE_PROFILES fehlt -> voice,tracing,web und ARGUS_ENV_VERSION=2
    (tmp_path / ".env").write_text(base_env, encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env1 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env1["COMPOSE_PROFILES"] == "voice,tracing,web"
    assert env1["ARGUS_ENV_VERSION"] == "2"

    # 2. Alte .env (kein ARGUS_ENV_VERSION) mit voice,tracing -> web wird angehaengt
    (tmp_path / ".env").write_text(base_env + "COMPOSE_PROFILES=voice,tracing\n", encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env2 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env2["COMPOSE_PROFILES"] == "voice,tracing,web"
    assert env2["ARGUS_ENV_VERSION"] == "2"

    # 3. Alte .env mit leerem COMPOSE_PROFILES= -> bekommt web
    (tmp_path / ".env").write_text(base_env + "COMPOSE_PROFILES=\n", encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env3 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env3["COMPOSE_PROFILES"] == "web"
    assert env3["ARGUS_ENV_VERSION"] == "2"

    # 4. Alte .env mit proxy -> proxy,web
    (tmp_path / ".env").write_text(base_env + "COMPOSE_PROFILES=proxy\n", encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env4 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env4["COMPOSE_PROFILES"] == "proxy,web"
    assert env4["ARGUS_ENV_VERSION"] == "2"

    # 5. Version 2 schon gesetzt mit leerem COMPOSE_PROFILES -> bleibt leer
    (tmp_path / ".env").write_text(base_env + "ARGUS_ENV_VERSION=2\nCOMPOSE_PROFILES=\n", encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env5 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env5["COMPOSE_PROFILES"] == ""

    # 6. Version 2 schon gesetzt mit proxy -> bleibt proxy
    (tmp_path / ".env").write_text(base_env + "ARGUS_ENV_VERSION=2\nCOMPOSE_PROFILES=proxy\n", encoding="utf-8")
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env6 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env6["COMPOSE_PROFILES"] == "proxy"


# =============================================================================
# 6. setup/check_networks.py: searxng und local-proxy optional
# =============================================================================


def test_check_networks_searxng_and_local_proxy_optional():
    net_mod = _load_setup_module("check_networks", "check_networks.py")

    def make_config(include_web: bool) -> dict:
        services = {
            "llm": {"networks": {"intern": None}},
            "embed": {"networks": {"intern": None}},
            "rerank": {"networks": {"intern": None}},
            "postgres": {"networks": {"intern": None}},
            "qdrant": {"networks": {"intern": None}},
            "calc-sandbox": {"networks": {"sandbox": None}},
            "intern-proxy": {
                "networks": {"intern": None, "publish": None},
                "ports": [{"host_ip": "127.0.0.1", "target": 6006, "published": "6006"}],
            },
            "rag-backend": {
                "networks": {"backend": None, "intern": None, "sandbox": None, "edge": None},
                "ports": [{"host_ip": "127.0.0.1", "target": 7860, "published": "7860"}],
            },
        }
        if include_web:
            services["searxng"] = {"networks": {"backend": None}}
            services["local-proxy"] = {
                "networks": {"backend": None},
                "ports": [{"host_ip": "127.0.0.1", "target": 8888, "published": "8888"}],
            }
        return {
            "networks": {
                "backend": {},
                "intern": {"internal": True},
                "publish": {},
                "sandbox": {"internal": True},
                "edge": {},
            },
            "services": services,
        }

    # Ohne searxng und local-proxy -> fehlerfrei
    assert net_mod.check(make_config(include_web=False)) == []

    # Mit searxng und local-proxy -> fehlerfrei
    assert net_mod.check(make_config(include_web=True)) == []


# =============================================================================
# 7. setup/install.py: --web/--no-web und doctor ARGUS_ENV_VERSION
# =============================================================================


def test_installer_web_flag_and_doctor_version_warning(tmp_path, monkeypatch):
    installer = _load_setup_module("install", "install.py")
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "setup" / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(REPO_ROOT / ".env.example", repo / ".env.example")
    shutil.copy2(REPO_ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    init_mod = installer._load_init_stack_module(repo)

    # update_compose_profiles mit use_web=False
    merged_off = installer.update_compose_profiles(repo, use_voice=True, use_tracing=True, use_web=False)
    assert "web" not in merged_off.split(",")
    assert "voice" in merged_off.split(",")

    # update_compose_profiles mit use_web=True
    merged_on = installer.update_compose_profiles(repo, use_voice=True, use_tracing=True, use_web=True)
    assert "web" in merged_on.split(",")

    # doctor_check_env meldet WARNUNG bei fehlender oder kleinerer ARGUS_ENV_VERSION
    ws = tmp_path / "ws"
    evals = tmp_path / "ws" / "Evals"
    models = tmp_path / "models"
    ws.mkdir(parents=True, exist_ok=True)
    evals.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)

    (repo / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\nARGUS_MODELS_DIR={models.as_posix()}\nARGUS_EVALS_DIR={evals.as_posix()}\n"
        "COMPOSE_PROFILES=voice,tracing,web\n",
        encoding="utf-8",
    )
    r_no_ver = installer.doctor_check_env(repo)
    assert r_no_ver.status == "WARNUNG"
    assert "ARGUS_ENV_VERSION" in r_no_ver.message
    assert "init_stack.py" in r_no_ver.message

    (repo / ".env").write_text(
        "ARGUS_ENV_VERSION=1\n"
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\nARGUS_MODELS_DIR={models.as_posix()}\nARGUS_EVALS_DIR={evals.as_posix()}\n"
        "COMPOSE_PROFILES=voice,tracing,web\n",
        encoding="utf-8",
    )
    r_old_ver = installer.doctor_check_env(repo)
    assert r_old_ver.status == "WARNUNG"
    assert "ARGUS_ENV_VERSION" in r_old_ver.message

    # doctor_check_stack: searxng und local-proxy sind in PROFILE_SERVICES['web']
    assert installer.PROFILE_SERVICES["web"] == ("searxng", "local-proxy")
