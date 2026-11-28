import os

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import httpx
import pytest

from rag_backend import egress_guard
from rag_backend import main as m
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)
from rag_backend.tests.test_sandbox_replicas import ADDR_A, sandbox_net  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_login_counters():
    m._LOGIN_FAILS.clear()
    yield
    m._LOGIN_FAILS.clear()


# ---- CORS -------------------------------------------------------------------

def test_cors_does_not_hand_out_chats_to_other_local_origins(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r = client.get("/v1/dashboard/chats", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


# ---- Grenzen fuer Request-Bodies --------------------------------------------

def test_oversized_body_is_rejected_before_login(client):
    big = {"username": "chef", "password": "x" * (2 * 1024 * 1024)}
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=big)
    assert r.status_code == 413


def test_chat_route_accepts_more_than_the_default_limit(client, monkeypatch):
    from rag_backend.tests.test_main import TestingSessionLocal
    monkeypatch.setattr(m, "SessionLocal", TestingSessionLocal)
    _make_account(client, "chef", "einLangesPasswort", "admin")

    async def _fake_astream(payload):
        if False:
            yield {}

    monkeypatch.setattr(m.agent_executor, "astream", _fake_astream)
    #--- Bis 200.000 Zeichen zu je 4 Byte: weit ueber der Standardgrenze von 1 MB.
    text = "\U0001F600" * m._MAX_MESSAGE_CHARS
    r = client.post("/v1/dashboard/chat", headers=DASH_ORIGIN, json={"message": text})
    assert r.status_code == 200
    assert '"type": "done"' in r.text


# ---- Anmeldesperre ----------------------------------------------------------

def test_failures_from_one_address_do_not_lock_other_addresses():
    for _ in range(m._LOGIN_MAX_FAILS):
        m._login_failed("chef", "10.0.0.9")
    assert m._login_locked("chef", "10.0.0.9") is True
    assert m._login_locked("chef", "10.0.0.20") is False


def test_many_addresses_lock_the_account_for_everyone():
    for i in range(m._LOGIN_MAX_FAILS_ACCOUNT):
        m._login_failed("chef", f"10.1.{i // 250}.{i % 250}")
    assert m._login_locked("chef", "192.168.5.5") is True


def test_prune_keeps_existing_locks(monkeypatch):
    monkeypatch.setattr(m, "_LOGIN_FAIL_ENTRIES_MAX", 50)
    for _ in range(m._LOGIN_MAX_FAILS):
        m._login_failed("chef", "10.0.0.9")
    for i in range(200):
        m._login_failed(f"fuellname{i}", "10.9.9.9")
    assert len(m._LOGIN_FAILS) <= 50 + 2
    assert m._login_locked("chef", "10.0.0.9") is True


def test_successful_login_clears_counter_of_this_address(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    for _ in range(m._LOGIN_MAX_FAILS - 1):
        r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                        json={"username": "chef", "password": "falsch-falsch"})
        assert r.status_code == 401
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                    json={"username": "chef", "password": "einLangesPasswort"})
    assert r.status_code == 200
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                    json={"username": "chef", "password": "falsch-falsch"})
    assert r.status_code == 401


def test_api_login_uses_lock_and_same_answer_for_unknown_accounts(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r_unknown = client.post("/login", data={"username": "niemand", "password": "x"})
    r_wrong = client.post("/login", data={"username": "chef", "password": "x"})
    assert r_unknown.status_code == r_wrong.status_code == 401
    assert r_unknown.json() == r_wrong.json()
    for _ in range(m._LOGIN_MAX_FAILS):
        client.post("/login", data={"username": "chef", "password": "x"})
    r = client.post("/login", data={"username": "chef", "password": "einLangesPasswort"})
    assert r.status_code == 429


def test_password_change_guessing_is_locked(client):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    for _ in range(m._LOGIN_MAX_FAILS):
        r = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                        json={"password": "nochLaengeresPasswort", "current_password": "geraten"})
        assert r.status_code == 403
    r = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                    json={"password": "nochLaengeresPasswort",
                          "current_password": "einLangesPasswort"})
    assert r.status_code == 429


# ---- Kontonamen ohne Gross-/Kleinschreibung ---------------------------------

def test_account_names_are_unique_ignoring_case(client):
    _make_account(client, "Chef", "einLangesPasswort", "admin")
    r = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                    json={"username": "chef", "password": "einLangesPasswort", "role": "user"})
    assert r.status_code == 409
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)
    r = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                    json={"username": "CHEF", "password": "einLangesPasswort"})
    assert r.status_code == 200
    assert r.json()["username"] == "Chef"


# ---- Dateien ----------------------------------------------------------------

def test_new_file_never_overwrites_and_never_follows_symlinks(tmp_path):
    from rag_backend import user_files
    first = user_files.write_new_file(tmp_path, "bericht.csv", b"eins")
    second = user_files.write_new_file(tmp_path, "bericht.csv", b"zwei")
    assert first.name == "bericht.csv" and second.name == "bericht_2.csv"
    assert first.read_bytes() == b"eins"
    outside = tmp_path / "ausserhalb.txt"
    (tmp_path / "falle.txt").symlink_to(outside)
    created = user_files.write_new_file(tmp_path, "falle.txt", b"daten")
    assert created.name == "falle_2.txt"
    assert not outside.exists()


# ---- Gespraech mit Firmendaten -----------------------------------------------

def test_chat_load_reports_sensitive_conversation(client, db_session):
    from rag_backend.models import ChatSession
    _make_account(client, "chef", "einLangesPasswort", "admin")
    cid = client.post("/v1/dashboard/chats", headers=DASH_ORIGIN).json()["id"]
    assert client.get(f"/v1/dashboard/chats/{cid}").json()["sensitive"] is False
    row = db_session.get(ChatSession, cid)
    row.has_sensitive_data = True
    db_session.commit()
    assert client.get(f"/v1/dashboard/chats/{cid}").json()["sensitive"] is True


def test_isolated_state_is_not_registered_and_starts_unmarked():
    before = dict(egress_guard._SESSION_STATES)
    state = egress_guard.activate_isolated_state("websuche")
    try:
        assert state.marked is False
        assert egress_guard.get_current_egress_state() is state
        assert dict(egress_guard._SESSION_STATES) == before
    finally:
        egress_guard.egress_state_var.set(None)


# ---- Sandbox-Token ----------------------------------------------------------

@pytest.mark.asyncio
async def test_sandbox_requests_carry_token(sandbox_net, monkeypatch):
    from rag_backend import agent
    monkeypatch.setenv("SANDBOX_TOKEN", "token-fuer-die-sandbox-0123456789")
    sandbox_net.handler = lambda request: httpx.Response(200, json={"result": "Ergebnis: 5"})
    resp = await agent._sandbox_post("/calculate", {"expression": "2+3"}, timeout=10.0)
    assert resp.status_code == 200
    assert sandbox_net.requests[0].url.host == ADDR_A
    assert sandbox_net.requests[0].headers.get("x-sandbox-token") == "token-fuer-die-sandbox-0123456789"


@pytest.mark.asyncio
async def test_sandbox_requests_without_configured_token(sandbox_net, monkeypatch):
    from rag_backend import agent
    monkeypatch.delenv("SANDBOX_TOKEN", raising=False)
    sandbox_net.handler = lambda request: httpx.Response(200, json={"result": "Ergebnis: 5"})
    await agent._sandbox_post("/calculate", {"expression": "2+3"}, timeout=10.0)
    assert "x-sandbox-token" not in sandbox_net.requests[0].headers


# ---- Qdrant-Schluessel ------------------------------------------------------

def test_qdrant_client_uses_api_key(monkeypatch):
    from rag_backend import qdrant_conn
    monkeypatch.setenv("QDRANT_API_KEY", "qdrant-test-schluessel")
    assert qdrant_conn.qdrant_api_key() == "qdrant-test-schluessel"
    monkeypatch.setenv("QDRANT_API_KEY", "  ")
    assert qdrant_conn.qdrant_api_key() is None
