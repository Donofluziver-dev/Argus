import hashlib
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

from rag_backend import siem
from rag_backend.action_engine import audit
from rag_backend.models import AuditLog, User
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    TestingSessionLocal,
    _make_account,
    override_get_db,
    test_client,
)


@pytest.fixture(autouse=True)
def setup_test_audit_env(tmp_path, monkeypatch):
    '''Richtet Anker-Datei, HMAC-Schluessel und DB-Session fuer Audit-Tests ein.'''
    anchor = tmp_path / "audit_anchor.json"
    monkeypatch.setenv("AUDIT_ANCHOR_PATH", str(anchor))
    monkeypatch.setattr(audit, "_HMAC_KEY", b"test_secret_audit_hmac_key_12345")
    monkeypatch.setattr("rag_backend.database.SessionLocal", TestingSessionLocal)

    from rag_backend.main import _LOGIN_FAILS, _LOGIN_FAIL_LOCK
    with _LOGIN_FAIL_LOCK:
        _LOGIN_FAILS.clear()

    siem.reset(flush=False)
    yield
    siem.reset(flush=False)


# ---- Teil A & B: Anmelde- und Konto-Ereignisse, Audit-Kette ------------------

def test_login_audit_success_failed_locked_no_password(client):
    '''Prueft Audit-Eintraege fuer Login: Erfolg, falsches Passwort, unbekanntes Konto, Sperre.'''
    _make_account(client, "adminuser", "StartPasswort123!", "admin")

    with TestingSessionLocal() as db:
        admin_row = db.query(User).filter_by(email="adminuser").first()
        admin_id = admin_row.id

    # 1. Erfolgreiche Anmeldung
    r_ok = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                       json={"username": "adminuser", "password": "StartPasswort123!"})
    assert r_ok.status_code == 200

    # 2. Falsches Passwort
    r_wrong = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                          json={"username": "adminuser", "password": "FalschesPasswort999!"})
    assert r_wrong.status_code == 401

    # 3. Unbekanntes Konto (Name darf NIE in details stehen)
    unknown_name = "unbekannter_nutzer"
    r_unk = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                        json={"username": unknown_name, "password": "GeheimesPasswort123!"})
    assert r_unk.status_code == 401

    # 4. Sperre nach Fehlversuchen provozieren
    from rag_backend.main import _LOGIN_MAX_FAILS, _login_failed
    for _ in range(_LOGIN_MAX_FAILS):
        _login_failed("adminuser")

    r_lock = client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                         json={"username": "adminuser", "password": "IrgendwasPasswort123!"})
    assert r_lock.status_code == 429

    # Audit-Eintraege pruefen
    with TestingSessionLocal() as db:
        rows = db.query(AuditLog).filter(AuditLog.tool == "auth.login").order_by(AuditLog.id.asc()).all()
        assert len(rows) >= 4

        # Letzte 4 Ereignisse auswerten: ok, wrong, unk, lock
        last_4 = rows[-4:]
        st_ok, st_wrong, st_unk, st_lock = last_4

        assert st_ok.status == "success"
        assert st_ok.user_id == admin_id
        det_ok = json.loads(st_ok.details)
        assert det_ok.get("account") == "adminuser"

        assert st_wrong.status == "failed"
        det_wrong = json.loads(st_wrong.details)
        assert det_wrong.get("reason") == "wrong_password"
        assert det_wrong.get("account") == "adminuser"

        assert st_unk.status == "failed"
        det_unk = json.loads(st_unk.details)
        assert det_unk.get("reason") == "unknown_account"
        # Name des unbekannten Kontos darf NICHT in details stehen
        assert "account" not in det_unk
        assert unknown_name not in st_unk.details
        # Dafuer muss der sha256-Hash in input_hash liegen
        expected_hash = hashlib.sha256(unknown_name.encode("utf-8")).hexdigest()[:16]
        assert st_unk.input_hash == expected_hash

        assert st_lock.status == "locked"
        det_lock = json.loads(st_lock.details)
        assert det_lock.get("account") == "adminuser"

        # Absicherung: Kein Passwort darf in irgendeinem Feld eines Audit-Eintrags stehen
        all_passwords = [
            "StartPasswort123!", "FalschesPasswort999!", "GeheimesPasswort123!",
            "IrgendwasPasswort123!"
        ]
        all_audit_rows = db.query(AuditLog).all()
        for r in all_audit_rows:
            for pwd in all_passwords:
                assert pwd not in (r.details or "")
                assert pwd not in (r.tool or "")
                assert pwd not in (r.tool_class or "")
                assert pwd not in (r.input_hash or "")
                assert pwd not in (r.output_hash or "")


def test_user_management_role_change_and_delete_audit(client):
    '''Prueft Rollenwechsel und Loeschen durch einen Administrator.'''
    _make_account(client, "chefadmin", "AdminPasswort1234!", "admin")

    with TestingSessionLocal() as db:
        admin_row = db.query(User).filter_by(email="chefadmin").first()
        admin_id = admin_row.id

    # Weiteren Nutzer anlegen
    r_create = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                           json={"username": "mitarbeiter", "password": "UserPasswort1234!", "role": "user"})
    assert r_create.status_code == 200
    target_id = r_create.json()["id"]

    # Rollenwechsel
    r_role = client.post(f"/v1/dashboard/users/{target_id}/role?role=admin", headers=DASH_ORIGIN)
    assert r_role.status_code == 200

    # Loeschen
    r_del = client.delete(f"/v1/dashboard/users/{target_id}", headers=DASH_ORIGIN)
    assert r_del.status_code == 200

    with TestingSessionLocal() as db:
        role_entry = db.query(AuditLog).filter_by(tool="account.role_change").order_by(AuditLog.id.desc()).first()
        assert role_entry is not None
        assert role_entry.status == "success"
        assert role_entry.user_id == admin_id
        det_role = json.loads(role_entry.details)
        assert det_role.get("target_id") == target_id
        assert det_role.get("old_role") == "user"
        assert det_role.get("new_role") == "admin"

        del_entry = db.query(AuditLog).filter_by(tool="account.delete").order_by(AuditLog.id.desc()).first()
        assert del_entry is not None
        assert del_entry.status == "success"
        assert del_entry.user_id == admin_id
        det_del = json.loads(del_entry.details)
        assert det_del.get("target_id") == target_id
        assert det_del.get("target_account") == "mitarbeiter"
        assert "chats_removed" in det_del


def test_verify_audit_chain_after_auth_events(client):
    '''Stellt sicher, dass die HMAC-Audit-Kette nach den neuen Ereignissen intakt bleibt.'''
    _make_account(client, "chainadmin", "KettenPasswort123!", "admin")
    client.post("/v1/dashboard/auth/login", headers=DASH_ORIGIN,
                json={"username": "chainadmin", "password": "KettenPasswort123!"})
    client.post("/v1/dashboard/auth/logout", headers=DASH_ORIGIN)

    res = audit.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] >= 2
    assert res["broken_id"] is None


# ---- Teil B: SIEM-Weiterleitung (JSON, CEF, UDP, TCP, Queue) -----------------

def test_siem_json_format():
    '''Prueft die JSON-Nachrichtenstruktur des SIEM-Moduls.'''
    record = {
        "ts": "2026-11-12T20:00:00.123456",
        "tool": "auth.login",
        "tool_class": "auth",
        "status": "success",
        "user_id": 42,
        "details": json.dumps({"ip": "192.168.1.100", "account": "alice"}),
        "input_hash": "a1b2c3d4e5f67890",
        "output_hash": "",
        "latency_ms": 12,
        "hmac": "sig123456",
    }
    iso_z, _ = siem._parse_ts(record["ts"])
    details_val, _ = siem._parse_details(record["details"])
    msg = siem._format_json(record, iso_z, "testhost", details_val)

    assert msg["type"] == "argus.audit"
    assert msg["ts"].endswith("Z")
    assert msg["host"] == "testhost"
    assert msg["event"] == "auth.login"
    assert msg["category"] == "auth"
    assert msg["status"] == "success"
    assert msg["user_id"] == 42
    assert isinstance(msg["details"], dict)
    assert msg["details"]["account"] == "alice"
    assert msg["input_hash"] == "a1b2c3d4e5f67890"
    assert msg["latency_ms"] == 12
    assert msg["hmac"] == "sig123456"


def test_siem_cef_escaping_and_format():
    '''Prueft CEF-Escaping im Header (| und \\) und in Werten (=, \\ und Zeilenumbrueche).'''
    header_raw = "test|pipe\\slash"
    escaped_header = siem._cef_escape_header(header_raw)
    assert escaped_header == "test\\|pipe\\\\slash"

    val_raw = "key=val\\slash\nsecondline\rthird"
    escaped_val = siem._cef_escape_value(val_raw)
    assert escaped_val == "key\\=val\\\\slash\\nsecondline\\nthird"

    record = {
        "ts": "2026-11-12T20:00:00.123456Z",
        "tool": "tool|special",
        "tool_class": "auth",
        "status": "canary_alarm",
        "user_id": 7,
        "details": json.dumps({"account": "bob", "ip": "10.0.0.1", "note": "val=1\\2\n3"}),
        "hmac": "sig=999\\end",
    }
    _, epoch_ms = siem._parse_ts(record["ts"])
    det_val, det_dict = siem._parse_details(record["details"])
    cef_sev, _ = siem._map_severity(record["status"])
    cef_str = siem._format_cef(record, epoch_ms, "1.0.0", cef_sev, det_val, det_dict)

    assert cef_str.startswith("CEF:0|Argus|Argus|1.0.0|tool\\|special|tool\\|special|10|")
    assert "suid=7" in cef_str
    assert "suser=bob" in cef_str
    assert "src=10.0.0.1" in cef_str
    assert "outcome=canary_alarm" in cef_str
    assert "cs1Label=details" in cef_str
    assert "cs2Label=hmac" in cef_str
    assert "cs2=sig\\=999\\\\end" in cef_str


def test_siem_udp_sending(monkeypatch):
    '''Sendet einen Audit-Eintrag ueber UDP an einen lokalen Test-Socket.'''
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.settimeout(3.0)

    monkeypatch.setenv("AUDIT_SIEM", "syslog")
    monkeypatch.setenv("AUDIT_SYSLOG_PROTOCOL", "udp")
    monkeypatch.setenv("AUDIT_SYSLOG_HOST", "127.0.0.1")
    monkeypatch.setenv("AUDIT_SYSLOG_PORT", str(port))
    monkeypatch.setenv("AUDIT_SIEM_FORMAT", "json")
    siem.reset()

    record = {
        "ts": "2026-11-12T20:00:00Z",
        "tool": "auth.login",
        "tool_class": "auth",
        "status": "success",
        "user_id": 1,
        "details": "{}",
        "hmac": "sig_udp",
    }
    siem.forward(record)

    try:
        data, _ = sock.recvfrom(4096)
        text = data.decode("utf-8")
        assert "1 2026-11-12T20:00:00Z" in text
        assert "argus - auth.login - " in text
        assert '"type": "argus.audit"' in text
    finally:
        sock.close()
        siem.reset()


def test_siem_tcp_octet_counting(monkeypatch):
    '''Sendet einen Audit-Eintrag ueber TCP mit RFC 6587 Octet-Counting.'''
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    server.settimeout(3.0)

    monkeypatch.setenv("AUDIT_SIEM", "syslog")
    monkeypatch.setenv("AUDIT_SYSLOG_PROTOCOL", "tcp")
    monkeypatch.setenv("AUDIT_SYSLOG_HOST", "127.0.0.1")
    monkeypatch.setenv("AUDIT_SYSLOG_PORT", str(port))
    monkeypatch.setenv("AUDIT_SIEM_FORMAT", "json")
    siem.reset()

    record = {
        "ts": "2026-11-12T20:00:00Z",
        "tool": "auth.login",
        "tool_class": "auth",
        "status": "failed",
        "user_id": 2,
        "details": "{}",
        "hmac": "sig_tcp",
    }
    siem.forward(record)

    try:
        conn, _ = server.accept()
        conn.settimeout(3.0)
        data = conn.recv(4096)
        conn.close()

        # Octet-Counting pruefen: "<Laenge> <Nachricht>"
        parts = data.split(b" ", 1)
        length_header = int(parts[0].decode("ascii"))
        payload = parts[1]
        assert len(payload) == length_header
        assert b"argus - auth.login - " in payload
    finally:
        server.close()
        siem.reset()


def test_siem_queue_overflow_non_blocking(monkeypatch):
    '''Verifiziert, dass eine volle Queue nicht blockiert und Verworfene zaehlt.'''
    monkeypatch.setenv("AUDIT_SIEM", "stdout")
    siem.reset()
    siem._init_siem()
    siem.stop()  # Worker anhalten, damit Queue nicht entleert wird

    # Warteschlange bis zum Anschlag fuellen
    while not siem._queue.full():
        siem._queue.put_nowait({"dummy": True})

    initial_drops = siem.get_dropped_count()
    start_time = time.time()
    # Neuer Eintrag darf nicht blockieren
    siem.forward({"overflow": True})
    elapsed = time.time() - start_time

    assert elapsed < 0.2
    assert siem.get_dropped_count() == initial_drops + 1
    siem.reset()


def test_siem_off_sends_nothing(monkeypatch):
    '''AUDIT_SIEM=off darf keine Threads starten und nichts in die Queue legen.'''
    monkeypatch.setenv("AUDIT_SIEM", "off")
    siem.reset()

    siem.forward({"ts": "2026-11-12T20:00:00Z", "tool": "test"})
    assert siem._worker_thread is None
    assert siem._queue.empty() is True
    siem.reset()


# ---- Teil D: Seitenabruf mit Groessengrenze (_fetch_clean_page) ---------------

@pytest.mark.asyncio
async def test_fetch_clean_page_content_length_limit(monkeypatch):
    '''Eine Seite mit Content-Length ueber der Grenze wird abgelehnt.'''
    from rag_backend import agent

    monkeypatch.setenv("WEBPAGE_MAX_BYTES", "1000")

    class MockStreamResponse:
        def __init__(self):
            self.headers = {"content-length": "2000"}
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def aiter_bytes(self):
            yield b"X" * 2000

    class MockClient:
        def stream(self, method, url):
            return MockStreamResponse()

    monkeypatch.setattr(agent, "_http_client_web", MockClient())
    monkeypatch.setattr(agent, "_ensure_models_loaded", AsyncMock())

    result = await agent._fetch_clean_page("https://example.com/huge")
    assert result == "ERROR: Page too large -- pick a different URL."


@pytest.mark.asyncio
async def test_fetch_clean_page_stream_truncation(monkeypatch):
    '''Antworten ueber der Grenze werden abgeschnitten und nie ganz gelesen.'''
    from rag_backend import agent

    monkeypatch.setenv("WEBPAGE_MAX_BYTES", "500")

    read_chunks = []

    class MockStreamResponse:
        def __init__(self):
            self.headers = {}
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def aiter_bytes(self):
            # 3 Chunks zu je 300 Bytes (insgesamt 900 Bytes > 500 Bytes Limit)
            for i in range(3):
                chunk = (f"<p>Chunk {i} mit Text zum Fuellen der Seite fuer den Test.</p>" * 5).encode("utf-8")[:300]
                read_chunks.append(i)
                yield chunk

    class MockClient:
        def stream(self, method, url):
            return MockStreamResponse()

    monkeypatch.setattr(agent, "_http_client_web", MockClient())
    monkeypatch.setattr(agent, "_ensure_models_loaded", AsyncMock())

    result = await agent._fetch_clean_page("https://example.com/stream-limit")
    # Es duerfen nur maximal 2 Chunks gelesen worden sein, der 3. Chunk nie
    assert len(read_chunks) == 2
    assert "Chunk 0" in result
    assert "Chunk 2" not in result


# ---- Zusaetzliche Absicherung aller Anmelde- und Kontorouten -----------------

def test_api_login_audit_events(client):
    '''Prueft /login fuer Erfolg, unbekanntes Konto, falsches Passwort und denied wegen Rolle.'''
    _make_account(client, "admin_api", "AdminApiPasswort123!", "admin")

    # 1. Unbekanntes Konto
    r1 = client.post("/login", data={"username": "unknown_api_user", "password": "any"})
    assert r1.status_code == 401

    # 2. Falsches Passwort
    r2 = client.post("/login", data={"username": "admin_api", "password": "wrong"})
    assert r2.status_code == 401

    # 3. Erfolg
    r3 = client.post("/login", data={"username": "admin_api", "password": "AdminApiPasswort123!"})
    assert r3.status_code == 200

    # 4. Nutzer ohne Admin-Rolle anlegen und /login versuchen (denied reason role)
    r_user = client.post("/v1/dashboard/users", headers=DASH_ORIGIN,
                         json={"username": "normal_user", "password": "NormalUserPasswort123!", "role": "user"})
    assert r_user.status_code == 200

    r4 = client.post("/login", data={"username": "normal_user", "password": "NormalUserPasswort123!"})
    assert r4.status_code == 403

    with TestingSessionLocal() as db:
        rows = db.query(AuditLog).filter(AuditLog.tool == "auth.api_login").order_by(AuditLog.id.asc()).all()
        assert len(rows) >= 4
        e_unk, e_wrong, e_ok, e_denied = rows[-4:]

        assert e_unk.status == "failed"
        det_unk = json.loads(e_unk.details)
        assert det_unk.get("reason") == "unknown_account"
        assert "account" not in det_unk

        assert e_wrong.status == "failed"
        det_wrong = json.loads(e_wrong.details)
        assert det_wrong.get("reason") == "wrong_password"
        assert det_wrong.get("account") == "admin_api"

        assert e_ok.status == "success"
        det_ok = json.loads(e_ok.details)
        assert det_ok.get("account") == "admin_api"

        assert e_denied.status == "denied"
        det_denied = json.loads(e_denied.details)
        assert det_denied.get("reason") == "role"
        assert det_denied.get("account") == "normal_user"


def test_dashboard_auth_setup_denied_token(client, monkeypatch):
    '''Prueft auth.setup mit ungueltigem Einrichtungs-Schluessel.'''
    monkeypatch.setenv("DASHBOARD_SETUP_TOKEN", "super-secret-token")
    body = {"username": "first_admin", "password": "AdminPassword123!"}

    # Ohne / mit falschem Token -> denied mit reason "setup_token"
    r_denied = client.post("/v1/dashboard/auth/setup", headers=DASH_ORIGIN, json=body)
    assert r_denied.status_code == 403

    with TestingSessionLocal() as db:
        entry = db.query(AuditLog).filter_by(tool="auth.setup", status="denied").first()
        assert entry is not None
        det = json.loads(entry.details)
        assert det.get("reason") == "setup_token"


def test_password_change_audit(client):
    '''Prueft auth.password_change bei falschem aktuellem Passwort und Erfolg.'''
    _make_account(client, "pwd_user", "OldPassword1234!", "admin")

    # 1. Falsches aktuelles Passwort
    r_fail = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                         json={"current_password": "WrongOldPassword!", "password": "NewPassword1234!"})
    assert r_fail.status_code == 403

    # 2. Erfolgreiche Aenderung
    r_ok = client.post("/v1/dashboard/auth/password", headers=DASH_ORIGIN,
                       json={"current_password": "OldPassword1234!", "password": "NewPassword1234!"})
    assert r_ok.status_code == 200

    with TestingSessionLocal() as db:
        entries = db.query(AuditLog).filter_by(tool="auth.password_change").order_by(AuditLog.id.asc()).all()
        assert len(entries) >= 2
        e_fail, e_success = entries[-2:]

        assert e_fail.status == "failed"
        det_fail = json.loads(e_fail.details)
        assert det_fail.get("reason") == "wrong_current_password"

        assert e_success.status == "success"
        det_ok = json.loads(e_success.details)
        assert det_ok.get("account") == "pwd_user"


def test_role_change_last_admin_denied(client):
    '''Prueft, dass der letzte Admin nicht degradiert werden darf (denied reason last_admin).'''
    _make_account(client, "sole_admin", "AdminPasswort123!", "admin")

    with TestingSessionLocal() as db:
        admin_row = db.query(User).filter_by(email="sole_admin").first()
        admin_id = admin_row.id

    r = client.post(f"/v1/dashboard/users/{admin_id}/role?role=user", headers=DASH_ORIGIN)
    assert r.status_code == 409

    with TestingSessionLocal() as db:
        entry = db.query(AuditLog).filter_by(tool="account.role_change", status="denied").first()
        assert entry is not None
        det = json.loads(entry.details)
        assert det.get("reason") == "last_admin"
        assert det.get("target_id") == admin_id


def test_audit_auth_forwarded_for_config(monkeypatch):
    '''Prueft X-Forwarded-For: wird nur aufgenommen wenn AUDIT_TRUST_FORWARDED=true.'''
    from rag_backend.security_audit import audit_auth

    calls = []

    def mock_write(**kwargs):
        calls.append(kwargs)
        return True

    from rag_backend.action_engine import audit as audit_mod
    monkeypatch.setattr(audit_mod, "write_audit_log", mock_write)

    class FakeRequest:
        client = MagicMock(host="10.10.10.10")
        headers = {"x-forwarded-for": "203.0.113.195, 198.51.100.17"}

    # 1. Vorgabe: false -> kein forwarded_for
    monkeypatch.delenv("AUDIT_TRUST_FORWARDED", raising=False)
    audit_auth("auth.login", status="success", request=FakeRequest(), account="testuser")
    d1 = json.loads(calls[-1]["details"])
    assert d1.get("ip") == "10.10.10.10"
    assert "forwarded_for" not in d1

    # 2. true -> forwarded_for vorhanden (erster Eintrag, max 64 Zeichen)
    monkeypatch.setenv("AUDIT_TRUST_FORWARDED", "true")
    audit_auth("auth.login", status="success", request=FakeRequest(), account="testuser")
    d2 = json.loads(calls[-1]["details"])
    assert d2.get("ip") == "10.10.10.10"
    assert d2.get("forwarded_for") == "203.0.113.195"



# ---- Nachtraege: Syslog-Kopf, stdout im MCP-Server, Zustellfehler ---------------

def test_syslog_header_fields_are_single_ascii_tokens():
    #--- Werkzeugnamen aus dem Koeder-Alarm koennen Leerzeichen und Umlaute tragen.
    assert siem._syslog_token("Webseiten Abruf für x", 32) == "Webseiten_Abruf_f_r_x"
    assert len(siem._syslog_token("a" * 80, 32)) == 32
    assert siem._syslog_token("", 32) == "-"


def test_stdout_mode_avoids_stdout_in_the_mcp_server(monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_SIEM", "stdout")
    siem.reset(flush=False)
    siem._init_siem()
    monkeypatch.setenv("ARGUS_STDOUT_RESERVED", "1")
    siem._dispatch_record({"ts": "2026-11-12T20:00:00Z", "tool": "fs_write_file",
                           "tool_class": "write", "status": "ok", "details": "x"})
    out = capsys.readouterr()
    assert out.out == "" and '"event": "fs_write_file"' in out.err
    monkeypatch.delenv("ARGUS_STDOUT_RESERVED")
    siem._dispatch_record({"ts": "2026-11-12T20:00:00Z", "tool": "auth.login",
                           "tool_class": "auth", "status": "success", "details": "{}"})
    assert '"event": "auth.login"' in capsys.readouterr().out
    siem.reset(flush=False)


def test_unreachable_tcp_siem_counts_undelivered_records(monkeypatch):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # niemand hoert auf diesem Port
    monkeypatch.setenv("AUDIT_SIEM", "syslog")
    monkeypatch.setenv("AUDIT_SYSLOG_PROTOCOL", "tcp")
    monkeypatch.setenv("AUDIT_SYSLOG_HOST", "127.0.0.1")
    monkeypatch.setenv("AUDIT_SYSLOG_PORT", str(port))
    siem.reset(flush=False)
    for i in range(3):
        siem.forward({"ts": "2026-11-12T20:00:00Z", "tool": f"t{i}", "tool_class": "auth",
                      "status": "failed", "details": "{}"})
    deadline = time.time() + 5
    while siem.get_dropped_count() < 3 and time.time() < deadline:
        time.sleep(0.05)
    #--- Verbindungsfehler und Eintraege in der Pause danach zaehlen beide.
    assert siem.get_dropped_count() == 3
    siem.reset(flush=False)


def test_mcp_subprocess_gets_siem_settings_but_no_secrets(monkeypatch):
    from rag_backend import mcp_loader
    monkeypatch.setenv("AUDIT_SIEM", "syslog")
    monkeypatch.setenv("AUDIT_SYSLOG_HOST", "siem.example")
    monkeypatch.setenv("JWT_SECRET", "geheim")
    env = mcp_loader._build_stdio_env(None)
    assert env["AUDIT_SIEM"] == "syslog" and env["AUDIT_SYSLOG_HOST"] == "siem.example"
    assert "JWT_SECRET" not in env
