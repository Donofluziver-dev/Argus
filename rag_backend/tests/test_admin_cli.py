"""Tests fuer das Administrations-Werkzeug rag_backend/admin_cli.py."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rag_backend import admin_cli, siem
from rag_backend.action_engine import audit
from rag_backend.models import AuditLog, Base, User

_ph = PasswordHasher()


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    """Frische SQLite-Datenbank und Audit-Umgebung je Test."""
    db_file = tmp_path / "admin_cli_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    anchor = tmp_path / "audit_anchor.json"
    monkeypatch.setenv("AUDIT_ANCHOR_PATH", str(anchor))
    monkeypatch.setattr(audit, "_HMAC_KEY", b"test_secret_audit_hmac_key_12345")
    monkeypatch.setattr("rag_backend.database.SessionLocal", SessionTesting)

    siem.reset(flush=False)
    try:
        with SessionTesting() as db:
            db.add(User(
                id=0,
                email="service-account@system.local",
                hashed_password="__service_account_no_login__",
                role="admin",
                token_version=0,
            ))
            db.add(User(
                id=1,
                email="admin_anna",
                hashed_password=_ph.hash("AltesPasswort123!"),
                role="admin",
                token_version=2,
            ))
            db.commit()
        yield SessionTesting
    finally:
        siem.reset(flush=False)
        engine.dispose()


def test_admin_cli_does_not_import_main():
    """rag_backend.admin_cli darf beim Import nicht rag_backend.main laden."""
    env = os.environ.copy()
    env.setdefault("DATABASE_URL", "sqlite:///:memory:")
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, rag_backend.admin_cli; assert 'rag_backend.main' not in sys.modules",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def test_reset_password_success_updates_hash_token_version_and_audit(cli_db, capsys):
    new_pw = "NeuesStakesPasswort2026!"
    rc = admin_cli.main(["reset-password", "admin_anna"], stdin=io.StringIO(new_pw + "\n"))
    assert rc == 0

    captured = capsys.readouterr()
    assert "admin_anna" in captured.out
    assert new_pw not in captured.out
    assert new_pw not in captured.err

    with cli_db() as db:
        user = db.query(User).filter_by(email="admin_anna").first()
        assert user is not None
        assert _ph.verify(user.hashed_password, new_pw) is True
        assert user.token_version == 3

        audit_row = (
            db.query(AuditLog)
            .filter_by(tool="account.password_reset")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        assert audit_row.status == "success"
        assert audit_row.user_id == 1
        details = json.loads(audit_row.details)
        assert details.get("source") == "cli"
        assert details.get("account") == "admin_anna"
        assert details.get("target_account") == "admin_anna"
        assert details.get("target_id") == 1
        # Kein Passwort im Audit-Protokoll
        assert new_pw not in (audit_row.details or "")
        assert new_pw not in (audit_row.input_hash or "")


def test_reset_password_rejects_short_password(cli_db, capsys):
    for short_pw in ("", "1234567", "kurz"):
        rc = admin_cli.main(["reset-password", "admin_anna"], stdin=io.StringIO(short_pw + "\n"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "8 Zeichen" in err

    with cli_db() as db:
        user = db.query(User).filter_by(email="admin_anna").first()
        assert _ph.verify(user.hashed_password, "AltesPasswort123!") is True
        assert user.token_version == 2
        assert db.query(AuditLog).filter_by(tool="account.password_reset").count() == 0


def test_reset_password_rejects_service_account(cli_db, capsys):
    for acct in ("service-account@system.local", "0"):
        rc = admin_cli.main(["reset-password", acct], stdin=io.StringIO("GueltigesPasswort123!\n"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "Dienstkonto" in err

    with cli_db() as db:
        svc = db.get(User, 0)
        assert svc.hashed_password == "__service_account_no_login__"
        assert svc.token_version == 0
        assert db.query(AuditLog).filter_by(tool="account.password_reset").count() == 0


def test_reset_password_unknown_account_returns_1(cli_db, capsys):
    rc = admin_cli.main(["reset-password", "unbekannt"], stdin=io.StringIO("GueltigesPasswort123!\n"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "unbekannt" in err.lower()
    with cli_db() as db:
        assert db.query(AuditLog).filter_by(tool="account.password_reset").count() == 0


def test_reset_password_rejects_password_as_cli_argument(cli_db):
    rc = admin_cli.main(
        ["reset-password", "admin_anna", "PasswortAlsArgument123!"],
        stdin=io.StringIO(""),
    )
    assert rc != 0
    with cli_db() as db:
        user = db.query(User).filter_by(email="admin_anna").first()
        assert user.token_version == 2
