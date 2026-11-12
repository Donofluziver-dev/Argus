'''Tests fuer die Post-Session-Reflektion (rag_backend/memory.py).
Prueft insbesondere das Verhalten, wenn ein Konto geloescht wird, waehrend die
Reflektion laeuft oder unmittelbar davor.
'''
import os
os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")

import pytest
from unittest.mock import AsyncMock
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rag_backend.models import Base, User, ReflectionState
from rag_backend.memory import EpisodicMemory

from sqlalchemy.pool import StaticPool

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(autouse=True)
def _setup_db(monkeypatch, tmp_path):
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr("rag_backend.database.SessionLocal", _TestingSessionLocal)
    monkeypatch.setenv("ARGUS_WORKSPACE", str(tmp_path))
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.mark.asyncio
async def test_reflection_aborts_if_user_deleted_before_write(monkeypatch, tmp_path):
    # Konto anlegen
    with _TestingSessionLocal() as db:
        user = User(id=42, email="testuser", hashed_password="dummy", role="user")
        db.add(user)
        db.commit()

    mem = EpisodicMemory()
    mem._ws = tmp_path

    # Nachrichten laden mocken (liefert gueltige Zeilen)
    rows = [("user", "Erinnerung: Ich mag Kaffee."), ("assistant", "Notiert.")]
    monkeypatch.setattr(mem, "_load_new_messages", lambda uid: (rows, 12345.0))

    # Fake LLM fuer _llm_fast
    fake_llm = AsyncMock()
    fake_llm.ainvoke.return_value = AIMessage(content="User mag Kaffee.")
    monkeypatch.setattr("rag_backend.agent._llm_fast", fake_llm)

    # Konto wird geloescht, nachdem Nachrichten geladen wurden, aber VOR dem Schreiben
    with _TestingSessionLocal() as db:
        u = db.get(User, 42)
        db.delete(u)
        db.commit()

    result = await mem.reflect(42)

    # Muss 0 zurueckgeben
    assert result == 0

    # MEMORY.md darf NICHT existieren
    mem_file = tmp_path / "users" / "42" / "MEMORY.md"
    assert not mem_file.exists()

    # ReflectionState darf nicht existieren
    with _TestingSessionLocal() as db:
        state = db.get(ReflectionState, 42)
        assert state is None


@pytest.mark.asyncio
async def test_helpers_suppress_write_and_watermark_for_deleted_user(tmp_path):
    mem = EpisodicMemory()
    mem._ws = tmp_path

    # User 999 existiert nicht in der DB
    assert mem._user_exists(999) is False

    # Direktes Schreiben unterdrueckt
    mem._write_memory(999, "Sollte nicht geschrieben werden\n")
    mem_file = tmp_path / "users" / "999" / "MEMORY.md"
    assert not mem_file.exists()

    # Direktes Setzen des Wasserzeichens unterdrueckt
    mem._set_watermark(999, 54321.0)
    with _TestingSessionLocal() as db:
        state = db.get(ReflectionState, 999)
        assert state is None
