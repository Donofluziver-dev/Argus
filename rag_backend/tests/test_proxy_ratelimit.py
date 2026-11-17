"""Tests fuer Auftrag C: Ratenlimit je Konto, echte Client-Adresse hinter
vertrauten Proxys, Cookie-Secure-Flag, Sandbox-Sperre und Netzpruefung."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

from rag_backend import agent, main as backend_main
from rag_backend.action_engine import audit
from rag_backend.models import AuditLog, User
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    TestingSessionLocal,
    _make_account,
    app as main_app,
    get_db as main_get_db,
    override_get_db,
    test_client,
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Bindet Audit-Log, DB-Session und Einrichtungs-Schluessel fuer Proxy-Tests."""
    anchor = tmp_path / "audit_anchor.json"
    monkeypatch.setenv("AUDIT_ANCHOR_PATH", str(anchor))
    monkeypatch.delenv("DASHBOARD_SETUP_TOKEN", raising=False)
    monkeypatch.setattr(audit, "_HMAC_KEY", b"test_secret_audit_hmac_key_12345")
    monkeypatch.setattr("rag_backend.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(backend_main, "SessionLocal", TestingSessionLocal)


@pytest.fixture()
def active_limiter(monkeypatch):
    """Schaltet den SlowAPI-Limiter fuer den Test ein und leert seinen Speicher."""
    limiters = {backend_main.limiter, getattr(main_app.state, "limiter", backend_main.limiter)}
    for lim in limiters:
        lim.reset()
        monkeypatch.setattr(lim, "enabled", True)
    with backend_main._LOGIN_FAIL_LOCK:
        backend_main._LOGIN_FAILS.clear()
    yield getattr(main_app.state, "limiter", backend_main.limiter)
    for lim in limiters:
        lim.reset()
    with backend_main._LOGIN_FAIL_LOCK:
        backend_main._LOGIN_FAILS.clear()


# ==============================================================================
# A. Schluessel-Funktion (_rate_limit_key) und Ratenlimit je Konto / Adresse
# ==============================================================================


def test_rate_limit_key_cookie_bearer_and_fallback(db_session):
    """Prueft _rate_limit_key fuer gueltiges Cookie, API-Token und ungueltige Tokens."""
    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        dash_token = backend_main._dashboard_session_token(uid=42, seconds=3600, tv=0)
        api_token = backend_main.create_token(uid=77, tv=0)
        expired_cookie = jwt.encode(
            {
                "sub": "42",
                "scope": "dashboard",
                "tv": 0,
                "exp": datetime.now(timezone.utc) - timedelta(seconds=10),
            },
            backend_main.JWT_SECRET,
            algorithm=backend_main.JWT_ALGORITHM,
        )
        wrong_sig_cookie = jwt.encode(
            {
                "sub": "42",
                "scope": "dashboard",
                "tv": 0,
                "exp": datetime.now(timezone.utc) + timedelta(seconds=3600),
            },
            "wrong-secret-key-at-least-32-chars-long!!",
            algorithm=backend_main.JWT_ALGORITHM,
        )

        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("10.20.30.40", 4000)) as c:
            # 1. Gueltiges Sitzungs-Cookie -> konto:<uid>
            req_cookie = c.build_request(
                "POST", "/v1/dashboard/chat", cookies={backend_main._DASHBOARD_COOKIE: dash_token}
            )
            # Starlette Request aus Scope bauen
            from starlette.requests import Request as StarletteRequest

            def _make_req(path: str, headers: list[tuple[bytes, bytes]], client_host: str = "10.20.30.40"):
                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": headers,
                    "client": (client_host, 12345),
                    "scheme": "http",
                    "server": ("127.0.0.1", 7860),
                    "query_string": b"",
                }
                return StarletteRequest(scope)

            r1 = _make_req(
                "/v1/dashboard/chat",
                [(b"cookie", f"{backend_main._DASHBOARD_COOKIE}={dash_token}".encode())],
            )
            assert backend_main._rate_limit_key(r1) == "konto:42"

            # 2. Gueltiges API-Bearer-Token (Scope nicht dashboard) -> api:<uid>
            r2 = _make_req(
                "/v1/missions",
                [(b"authorization", f"Bearer {api_token}".encode())],
            )
            assert backend_main._rate_limit_key(r2) == "api:77"

            # 3. Sitzungs-Cookie als Bearer-Token missbraucht -> zaehlt nach Adresse
            r3 = _make_req(
                "/v1/missions",
                [(b"authorization", f"Bearer {dash_token}".encode())],
            )
            assert backend_main._rate_limit_key(r3) == "addr:10.20.30.40"

            # 4. API-Token als Sitzungs-Cookie missbraucht -> zaehlt nach Adresse
            r4 = _make_req(
                "/v1/dashboard/chat",
                [(b"cookie", f"{backend_main._DASHBOARD_COOKIE}={api_token}".encode())],
            )
            assert backend_main._rate_limit_key(r4) == "addr:10.20.30.40"

            # 5. Abgelaufenes oder falsch signiertes Cookie -> zaehlt nach Adresse
            for bad in (expired_cookie, wrong_sig_cookie, "kein-jwt"):
                r_bad = _make_req(
                    "/v1/dashboard/chat",
                    [(b"cookie", f"{backend_main._DASHBOARD_COOKIE}={bad}".encode())],
                )
                assert backend_main._rate_limit_key(r_bad) == "addr:10.20.30.40"

            # 6. Anmelde-, Einrichtungs- und Registrierungsrouten zaehlen immer nach Adresse
            for auth_path in ("/v1/dashboard/auth/login", "/v1/dashboard/auth/setup", "/v1/dashboard/auth/register", "/login", "/register"):
                r_auth = _make_req(
                    auth_path,
                    [(b"cookie", f"{backend_main._DASHBOARD_COOKIE}={dash_token}".encode())],
                )
                assert backend_main._rate_limit_key(r_auth) == "addr:10.20.30.40"
    finally:
        main_app.dependency_overrides.clear()


def test_two_accounts_same_address_do_not_share_rate_limit(db_session, active_limiter, monkeypatch):
    """Zwei Konten hinter derselben IP-Adresse teilen sich ihr Ratenlimit nicht."""
    monkeypatch.setenv("DASHBOARD_WEB_SEARCH_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(agent, "_search_and_read", AsyncMock(return_value="Suchtreffer"))
    monkeypatch.setattr(agent, "quellen_liste", lambda: [])
    monkeypatch.setattr(agent, "quellen_zuruecksetzen", lambda: None)

    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("192.0.2.50", 5000)) as admin_client:
            _make_account(admin_client, "alice", "PasswortAlice123!", "admin")
            r_create = admin_client.post(
                "/v1/dashboard/users",
                headers=DASH_ORIGIN,
                json={"username": "bob", "password": "PasswortBob12345!", "role": "user"},
            )
            assert r_create.status_code == 200
            alice_cookie = admin_client.cookies.get(backend_main._DASHBOARD_COOKIE)

        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("192.0.2.50", 5001)) as bob_client:
            r_login = bob_client.post(
                "/v1/dashboard/auth/login",
                headers=DASH_ORIGIN,
                json={"username": "bob", "password": "PasswortBob12345!"},
            )
            assert r_login.status_code == 200
            bob_cookie = bob_client.cookies.get(backend_main._DASHBOARD_COOKIE)

        active_limiter.reset()

        # Beide senden von derselben Gegenstelle 192.0.2.50
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("192.0.2.50", 6000)) as c_alice:
            c_alice.cookies.set(backend_main._DASHBOARD_COOKIE, alice_cookie)
            assert c_alice.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "q1"}).status_code == 200
            assert c_alice.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "q2"}).status_code == 200
            # 3. Anfrage von Alice wird gedrosselt (429)
            assert c_alice.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "q3"}).status_code == 429

        # Bob von derselben Adresse 192.0.2.50 hat sein eigenes Kontingent
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("192.0.2.50", 6001)) as c_bob:
            c_bob.cookies.set(backend_main._DASHBOARD_COOKIE, bob_cookie)
            assert c_bob.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "b1"}).status_code == 200
            assert c_bob.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "b2"}).status_code == 200
            assert c_bob.post("/v1/dashboard/web_search", headers=DASH_ORIGIN, json={"query": "b3"}).status_code == 429
    finally:
        main_app.dependency_overrides.clear()


def test_without_session_and_invalid_cookie_count_by_address(db_session, active_limiter, monkeypatch):
    """Ohne Sitzung sowie mit ungueltigem Cookie zaehlt die Absender-Adresse."""
    monkeypatch.setenv("DASHBOARD_LOGIN_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("DASHBOARD_WEB_SEARCH_RATE_LIMIT", "2/minute")

    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("198.51.100.10", 5000)) as c:
            _make_account(c, "chef", "PasswortChef123!", "admin")
            c.cookies.clear()
            active_limiter.reset()

            # 1. Ohne Sitzung: Login zaehlt nach Adresse 198.51.100.10
            login_payload = {"username": "chef", "password": "PasswortChef123!"}
            assert c.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 200
            c.cookies.clear()
            assert c.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 200
            c.cookies.clear()
            assert c.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 429

        # Andere Adresse ist nicht betroffen
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("198.51.100.11", 5000)) as c_other:
            assert c_other.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 200

        # 2. Ungueltige Cookies von derselben Adresse teilen sich den Adress-Zaehler
        active_limiter.reset()
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("198.51.100.25", 5000)) as c_bad:
            c_bad.cookies.set(backend_main._DASHBOARD_COOKIE, "ungueltig-eins")
            # Vor dem Limit weist dashboard_user mit 401 ab, aber SlowAPI zaehlt in der Route erst bei Aufruf:
            # Deshalb an einer Route pruefen oder ueber abgelaufenes/ungueltiges Cookie an login/web_search:
            # Bei login wird das ungueltige Cookie mitgeschickt und nach Adresse gezaehlt:
            assert c_bad.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 200
            c_bad.cookies.clear()
            c_bad.cookies.set(backend_main._DASHBOARD_COOKIE, "ungueltig-zwei")
            assert c_bad.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 200
            c_bad.cookies.clear()
            c_bad.cookies.set(backend_main._DASHBOARD_COOKIE, "ungueltig-drei")
            assert c_bad.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_payload).status_code == 429
    finally:
        main_app.dependency_overrides.clear()


# ==============================================================================
# B. Echte Client-Adresse und Schema nur von vertrauten Proxys
# ==============================================================================


def test_x_forwarded_for_only_trusted_from_configured_proxies(db_session, active_limiter, monkeypatch):
    """X-Forwarded-For wirkt fuer Ratenlimit und Audit nur, wenn die direkte Gegenstelle
    in ARGUS_TRUSTED_PROXIES steht."""
    monkeypatch.setenv("DASHBOARD_LOGIN_RATE_LIMIT", "1/minute")
    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860") as setup_c:
            _make_account(setup_c, "admin1", "PasswortAdmin123!", "admin")

        login_body = {"username": "admin1", "password": "PasswortAdmin123!"}

        # 1. Standard (ARGUS_TRUSTED_PROXIES leer): X-Forwarded-For wird ignoriert
        monkeypatch.delenv("ARGUS_TRUSTED_PROXIES", raising=False)
        active_limiter.reset()
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("10.0.0.5", 4321)) as untrusted_c:
            r1 = untrusted_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-For": "203.0.113.1"},
                json=login_body,
            )
            assert r1.status_code == 200
            untrusted_c.cookies.clear()
            # Zweite Anfrage mit gefaelschtem neuem X-Forwarded-For scheitert trotzdem am Limit von 10.0.0.5
            r2 = untrusted_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-For": "203.0.113.2"},
                json=login_body,
            )
            assert r2.status_code == 429

        # Im Audit-Log steht die echte Gegenstelle 10.0.0.5, nicht 203.0.113.1
        last_audit = db_session.query(AuditLog).filter_by(tool="auth.login", status="success").order_by(AuditLog.id.desc()).first()
        assert json.loads(last_audit.details)["ip"] == "10.0.0.5"

        # 2. Gegenstelle steht im CIDR-Netz von ARGUS_TRUSTED_PROXIES: X-Forwarded-For wird ausgewertet
        monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "172.31.250.0/29")
        active_limiter.reset()
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("172.31.250.2", 4321)) as proxy_c:
            r_a = proxy_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-For": "198.51.100.77"},
                json=login_body,
            )
            assert r_a.status_code == 200
            proxy_c.cookies.clear()

            # Zweiter Client hinter demselben Proxy hat eigene Adresse 198.51.100.88 -> 200 OK
            r_b = proxy_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-For": "198.51.100.88"},
                json=login_body,
            )
            assert r_b.status_code == 200
            proxy_c.cookies.clear()

            # Wiederholung fuer 198.51.100.77 -> 429
            r_a2 = proxy_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-For": "198.51.100.77"},
                json=login_body,
            )
            assert r_a2.status_code == 429

        # Im Audit-Log steht nun die Client-Adresse aus X-Forwarded-For
        recent_audits = (
            db_session.query(AuditLog)
            .filter_by(tool="auth.login", status="success")
            .order_by(AuditLog.id.desc())
            .limit(2)
            .all()
        )
        ips = {json.loads(a.details)["ip"] for a in recent_audits}
        assert ips == {"198.51.100.77", "198.51.100.88"}
    finally:
        main_app.dependency_overrides.clear()


def test_cookie_secure_flag_and_scheme_behind_proxy(db_session, monkeypatch):
    """Schema (request.url.scheme) kommt nur von einer vertrauten Gegenstelle. Das
    Secure-Flag folgt X-Forwarded-Proto: https von jeder Gegenstelle, weil es das
    Cookie nur strenger macht."""
    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860") as setup_c:
            _make_account(setup_c, "secuser", "PasswortSec1234!", "admin")

        login_body = {"username": "secuser", "password": "PasswortSec1234!"}
        monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "172.31.250.0/29")

        # 0. Klartext ohne Proxy-Header -> kein Secure-Flag
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("10.99.0.5", 1234)) as plain_c:
            r_plain = plain_c.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN, json=login_body)
            assert r_plain.status_code == 200
            assert "secure" not in r_plain.headers.get("set-cookie", "").lower()

        # 1. Nicht vertraute Gegenstelle schickt X-Forwarded-Proto: https -> Schema bleibt
        #    http (test_proxy_headers_middleware_*), das Cookie wird trotzdem Secure
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("10.99.0.5", 1234)) as untrusted_c:
            r_untrusted = untrusted_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-Proto": "https"},
                json=login_body,
            )
            assert r_untrusted.status_code == 200
            set_cookie_untrusted = r_untrusted.headers.get("set-cookie", "")
            assert "argus_session=" in set_cookie_untrusted
            assert "; secure" in set_cookie_untrusted.lower()

        # 2. Vertraute Gegenstelle schickt X-Forwarded-Proto: https -> Secure-Flag gesetzt
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("172.31.250.3", 1234)) as trusted_c:
            r_trusted = trusted_c.post(
                "/v1/dashboard/auth/login",
                headers={**DASH_ORIGIN, "X-Forwarded-Proto": "https"},
                json=login_body,
            )
            assert r_trusted.status_code == 200
            set_cookie_trusted = r_trusted.headers.get("set-cookie", "")
            assert "argus_session=" in set_cookie_trusted
            assert "; secure" in set_cookie_trusted.lower()
    finally:
        main_app.dependency_overrides.clear()


def _scope_after_proxy_headers(client_host: str, headers: list[tuple[bytes, bytes]]) -> dict:
    """Schickt einen HTTP-Scope durch _TrustedProxyHeaders und gibt zurueck, was die
    App dahinter sieht."""
    seen: dict = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    mw = backend_main._TrustedProxyHeaders(inner)
    scope = {"type": "http", "client": (client_host, 1234), "scheme": "http", "headers": headers}
    asyncio.run(mw(scope, None, None))
    return seen


def test_proxy_headers_middleware_trusts_only_configured_proxies(monkeypatch):
    fwd = [(b"x-forwarded-for", b"198.51.100.7"), (b"x-forwarded-proto", b"https")]

    monkeypatch.delenv("ARGUS_TRUSTED_PROXIES", raising=False)
    seen = _scope_after_proxy_headers("172.31.250.2", fwd)
    assert seen["client"][0] == "172.31.250.2" and seen["scheme"] == "http"

    monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "172.31.250.0/29")
    seen = _scope_after_proxy_headers("172.31.250.2", fwd)
    assert seen["client"][0] == "198.51.100.7" and seen["scheme"] == "https"
    seen = _scope_after_proxy_headers("10.99.0.5", fwd)
    assert seen["client"][0] == "10.99.0.5" and seen["scheme"] == "http"

    #--- '*' wuerde jeder Gegenstelle trauen und wird verworfen.
    monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "*")
    seen = _scope_after_proxy_headers("10.99.0.5", fwd)
    assert seen["client"][0] == "10.99.0.5" and seen["scheme"] == "http"


def test_proxy_headers_middleware_rereads_setting_at_runtime(monkeypatch):
    fwd = [(b"x-forwarded-for", b"198.51.100.7")]
    seen: dict = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    monkeypatch.delenv("ARGUS_TRUSTED_PROXIES", raising=False)
    mw = backend_main._TrustedProxyHeaders(inner)
    asyncio.run(mw({"type": "http", "client": ("10.0.0.9", 1), "scheme": "http", "headers": fwd}, None, None))
    assert seen["client"][0] == "10.0.0.9"

    monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "10.0.0.9")
    asyncio.run(mw({"type": "http", "client": ("10.0.0.9", 1), "scheme": "http", "headers": fwd}, None, None))
    assert seen["client"][0] == "198.51.100.7"


def test_sandbox_block_sees_real_peer_even_if_sandbox_net_is_trusted(db_session, monkeypatch):
    """Steht das Sandbox-Netz versehentlich in ARGUS_TRUSTED_PROXIES, kann die Sandbox
    sich trotzdem nicht per X-Forwarded-For als anderer Client ausgeben."""
    monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "172.16.0.0/12")
    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset({"172.19.0.55"}))
    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("172.19.0.55", 55555)) as sb_client:
            r = sb_client.get("/v1/dashboard/auth/state", headers={"X-Forwarded-For": "198.51.100.9"})
            assert r.status_code == 403
            r = sb_client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})
            assert r.status_code == 403
    finally:
        main_app.dependency_overrides.clear()


def test_sandbox_block_cannot_be_bypassed_with_x_forwarded_for(db_session, monkeypatch):
    """Die Sandbox-Sperre greift weiter, auch wenn die Sandbox X-Forwarded-For mitschickt."""
    monkeypatch.setenv("ARGUS_TRUSTED_PROXIES", "172.31.250.0/29")
    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset({"172.19.0.55"}))
    main_app.dependency_overrides[main_get_db] = lambda: db_session
    try:
        with TestClient(main_app, base_url="http://127.0.0.1:7860", client=("172.19.0.55", 55555)) as sb_client:
            r = sb_client.get(
                "/v1/dashboard/auth/state",
                headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
            )
            assert r.status_code == 403
            assert "Sandbox" in r.json().get("detail", "")
    finally:
        main_app.dependency_overrides.clear()


# ==============================================================================
# D. Netzpruefung (setup/check_networks.py)
# ==============================================================================


def _load_check_networks():
    script = Path(__file__).resolve().parents[2] / "setup" / "check_networks.py"
    spec = importlib.util.spec_from_file_location("check_networks", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_base_compose_cfg(with_caddy: bool = True) -> dict:
    services = {
        name: {"networks": {"intern": None}}
        for name in ("llm", "embed", "rerank", "postgres", "qdrant", "phoenix", "tts-service", "stt-service")
    }
    services["calc-sandbox"] = {"networks": {"sandbox": None}}
    services["searxng"] = {"networks": {"backend": None}}
    services["local-proxy"] = {
        "networks": {"backend": None},
        "ports": [{"host_ip": "127.0.0.1", "target": 8888, "published": "8888"}],
    }
    services["intern-proxy"] = {
        "networks": {"intern": None, "publish": None},
        "ports": [{"host_ip": "127.0.0.1", "target": 6006, "published": "6006"}],
    }
    services["rag-backend"] = {
        "networks": {"backend": None, "intern": None, "sandbox": None, "edge": None},
        "ports": [{"host_ip": "127.0.0.1", "target": 7860, "published": "7860"}],
    }
    if with_caddy:
        services["caddy"] = {
            "networks": {"edge": None},
            "ports": [
                {"host_ip": "0.0.0.0", "target": 80, "published": "80"},
                {"host_ip": "0.0.0.0", "target": 443, "published": "443"},
            ],
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


def test_check_networks_rules_for_edge_and_caddy():
    """Prueft die neuen Regeln in setup/check_networks.py:
    - Mit und ohne caddy gueltig
    - Dritter Dienst in 'edge' -> Fehler
    - Caddy auf Container-Port 8080 statt 443 -> Fehler
    - Anderer Dienst auf 0.0.0.0 -> Fehler
    """
    net_mod = _load_check_networks()

    # 1. Gueltig ohne und mit caddy
    assert net_mod.check(_valid_base_compose_cfg(with_caddy=False)) == []
    assert net_mod.check(_valid_base_compose_cfg(with_caddy=True)) == []

    # 2. Ein dritter Dienst in 'edge' -> Fehler
    cfg_third = _valid_base_compose_cfg(with_caddy=True)
    cfg_third["services"]["searxng"]["networks"]["edge"] = None
    errs_third = net_mod.check(cfg_third)
    assert any("edge" in e and "searxng" in e for e in errs_third), errs_third

    # 3. Caddy auf Container-Port 8080 statt 443 -> Fehler
    cfg_port = _valid_base_compose_cfg(with_caddy=True)
    cfg_port["services"]["caddy"]["ports"] = [
        {"host_ip": "0.0.0.0", "target": 8080, "published": "8080"}
    ]
    errs_port = net_mod.check(cfg_port)
    assert any("caddy" in e and "8080" in e for e in errs_port), errs_port

    # 4. Ein anderer Dienst veroeffentlicht Port auf 0.0.0.0 -> Fehler
    cfg_bind = _valid_base_compose_cfg(with_caddy=False)
    cfg_bind["services"]["rag-backend"]["ports"] = [
        {"host_ip": "0.0.0.0", "target": 7860, "published": "7860"}
    ]
    errs_bind = net_mod.check(cfg_bind)
    assert any("rag-backend" in e and "127.0.0.1" in e for e in errs_bind), errs_bind
