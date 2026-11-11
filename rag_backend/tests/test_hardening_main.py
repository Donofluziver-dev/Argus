import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

from pathlib import Path

import pytest
from pydantic import BaseModel

#--- Fixtures aus test_main wiederverwenden: Import registriert sie hier.
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)


def _chat_account(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                    json={"username": "gast", "password": "einLangesPasswort", "role": "user"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---- Registrierung ----------------------------------------------------------

def test_registration_is_closed_without_explicit_switch(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_REGISTER", raising=False)
    _make_account(client, "chef", "einLangesPasswort", "admin")
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    assert client.get("/v1/dashboard/auth/state").json()["allow_register"] is False
    r = client.post("/v1/dashboard/auth/register", headers=DASH_ORIGIN,
                    json={"username": "fremd", "password": "einLangesPasswort"})
    assert r.status_code == 403


def test_setup_token_guards_first_admin(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SETUP_TOKEN", "einmal-geheim")
    body = {"username": "chef", "password": "einLangesPasswort"}
    r = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN, json=body)
    assert r.status_code == 403
    r = client.post("/v1/dashboard/auth/setup",
                    headers={**DASH_ORIGIN, "X-Setup-Token": "einmal-geheim"}, json=body)
    assert r.status_code == 200, r.text


# ---- Sitzungen widerrufen ---------------------------------------------------

def test_logout_revokes_copied_cookie(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    stolen = client.cookies.get("argus_session")
    assert client.get("/v1/dashboard/chats").status_code == 200
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    client.cookies.clear()
    client.cookies.set("argus_session", stolen)
    assert client.get("/v1/dashboard/chats").status_code == 401


def test_password_change_needs_current_password_and_ends_other_sessions(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    other = client.cookies.get("argus_session")
    r = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                    json={"password": "nochLaengeresPasswort"})
    assert r.status_code == 403
    r = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                    json={"password": "nochLaengeresPasswort",
                          "current_password": "einLangesPasswort"})
    assert r.status_code == 200, r.text
    #--- Diese Sitzung bleibt (frisches Cookie), die alte ist ungueltig.
    assert client.get("/v1/dashboard/chats").status_code == 200
    client.cookies.clear()
    client.cookies.set("argus_session", other)
    assert client.get("/v1/dashboard/chats").status_code == 401


def test_login_locks_account_after_repeated_failures(client):
    from rag_backend import main as m
    _make_account(client, "sperrkonto", "einLangesPasswort", "admin")
    try:
        for _ in range(m._LOGIN_MAX_FAILS):
            r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                            json={"username": "sperrkonto", "password": "falsch-falsch"})
            assert r.status_code == 401
        #--- Auch das richtige Passwort hilft waehrend der Sperre nicht.
        r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                        json={"username": "sperrkonto", "password": "einLangesPasswort"})
        assert r.status_code == 429
    finally:
        m._LOGIN_FAILS.clear()


# ---- Missionen und API-Zugang -----------------------------------------------

def test_legacy_api_login_is_admin_only(client):
    _chat_account(client)
    r = client.post("/login", data={"username": "gast", "password": "einLangesPasswort"})
    assert r.status_code == 403
    r = client.post("/login", data={"username": "chef", "password": "einLangesPasswort"})
    assert r.status_code == 200


def test_missions_need_admin(client):
    from rag_backend.main import create_token
    uid = _chat_account(client)
    r = client.get("/v1/missions", headers={"Authorization": f"Bearer {create_token(uid)}"})
    assert r.status_code == 403


# ---- Werkzeuge nach Rolle ---------------------------------------------------

class _Tool:
    def __init__(self, name):
        self.name = name


def test_tools_for_role_hides_cloud_agents_files_and_sandbox():
    from rag_backend.agent import _tools_for_role
    names = ["web_search", "document_search", "cloud_ask", "start_mission", "invoke_subagent",
             "index_document", "fs_write_file", "fs_read_file", "execute_code", "read_document",
             "save_artifact", "calculate", "list_indexed_documents"]
    tools = [_Tool(n) for n in names]
    user = {t.name for t in _tools_for_role(tools, "user")}
    assert user == {"web_search", "document_search", "calculate", "list_indexed_documents"}
    #--- Alles ausser 'admin' bekommt die kleine Auswahl, auch eine unbekannte Rolle.
    assert {t.name for t in _tools_for_role(tools, "chat")} == user
    assert {t.name for t in _tools_for_role(tools, "")} == user
    assert _tools_for_role(tools, "admin") == tools
    assert _tools_for_role(tools, None) == tools


class _In(BaseModel):
    x: int = 0


@pytest.mark.asyncio
async def test_action_engine_refuses_chat_role():
    from rag_backend.action_engine.base_tool import (
        CURRENT_USER_ROLE, BaseTool, ToolClassification, ToolOutput)

    class Probe(BaseTool):
        name = "probe"
        classification = ToolClassification.READ

        def get_input_schema(self):
            return _In

        async def _execute(self, params):
            return ToolOutput(success=True, data="ran")

    token = CURRENT_USER_ROLE.set("user")
    try:
        out = await Probe().run({})
    finally:
        CURRENT_USER_ROLE.reset(token)
    assert out.success is False and "administrators" in out.error


# ---- Workspace-Schreibschutz --------------------------------------------------

def test_fs_write_protects_evals_and_security_state(tmp_path, monkeypatch):
    from rag_backend.mcp_servers import filesystem as fs
    monkeypatch.setattr(fs, "WORKSPACE_CONTAINER", Path(tmp_path))
    for rel in ("Evals/run_gaia.py", "EVALS/neu.py", "evals/Action_Audit/a.jsonl",
                "identity/SOUL.md", "IDENTITY/x.md", ".argus_known_hosts"):
        with pytest.raises(PermissionError):
            fs._enforce_writable((Path(tmp_path) / rel).resolve())
    #--- Das Gedaechtnis bleibt schreibbar.
    fs._enforce_writable((Path(tmp_path) / "users" / "1" / "MEMORY.md").resolve())


# ---- Koeder-Tokens ----------------------------------------------------------

CANARY = "argus-koeder-7f3c9a1e5b"


def test_canary_blocks_cloud_payload(monkeypatch):
    from rag_backend.cloud_agents import redactor
    monkeypatch.setenv("CANARY_TOKENS", CANARY)
    assert redactor.tripwire(f"Bitte pruefe {CANARY}") == "Koeder-Token im Payload"
    assert redactor.tripwire("Bitte pruefe den Text") is None


def test_canary_ignores_short_tokens(monkeypatch):
    from rag_backend.canary import check_canary
    monkeypatch.setenv("CANARY_TOKENS", "kurz")
    assert check_canary("das ist kurz", "test") is False


@pytest.mark.asyncio
async def test_canary_blocks_action_engine_params(monkeypatch):
    from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput

    class _P(BaseModel):
        text: str = ""

    class Probe(BaseTool):
        name = "probe"
        classification = ToolClassification.READ
        admin_only = False

        def get_input_schema(self):
            return _P

        async def _execute(self, params):
            return ToolOutput(success=True, data="ran")

    monkeypatch.setenv("CANARY_TOKENS", CANARY)
    out = await Probe().run({"text": f"exfil {CANARY}"})
    assert out.success is False and "protected data" in out.error
