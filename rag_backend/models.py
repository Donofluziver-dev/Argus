from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, LargeBinary, Boolean, Float
from sqlalchemy.orm import DeclarativeBase, relationship

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    #--- Wer schreibende Host-Aktionen freigeben darf. Kommt ueber
    #--- ensure_user_role_column statt ueber eine Alembic-Migration.
    role = Column(String, nullable=False, server_default="user", default="user")
    #--- Steht in jedem ausgestellten Token. Abmelden und Passwortwechsel zaehlen
    #--- hoch und machen damit alle aelteren Sitzungen ungueltig.
    token_version = Column(Integer, nullable=False, server_default="0", default=0)
    sessions = relationship("ChatSession", back_populates="user")


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    has_sensitive_data = Column(Boolean, nullable=False, server_default="0", default=False)
    messages = relationship("Message", back_populates="session")
    user = relationship("User", back_populates="sessions")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"))
    sender = Column(String)
    content_encrypted = Column(LargeBinary)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    latency_ms = Column(Integer, nullable=True)
    ttft_ms = Column(Integer, nullable=True)
    tokens_per_sec = Column(Float, nullable=True)
    #--- Werkzeugzeilen, Denktext und Quellen als verschluesseltes JSON.
    #    Verschluesselt wie der Inhalt: der Denktext ist oft persoenlicher
    #    als die Antwort, und in den Werkzeugzeilen stehen Suchanfragen.
    steps_encrypted = Column(LargeBinary, nullable=True)
    session = relationship("ChatSession", back_populates="messages")


class IngestedDocument(Base):
    __tablename__ = "ingested_documents"
    id = Column(Integer, primary_key=True)
    filename = Column(String, unique=True, index=True)
    file_path = Column(String)
    file_hash = Column(String)
    chunk_count = Column(Integer)
    ingested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    user_id = Column(Integer, nullable=True)
    tool = Column(String, nullable=False, index=True)
    tool_class = Column(String, nullable=False)
    input_hash = Column(String, nullable=False)
    output_hash = Column(String, nullable=True)
    status = Column(String, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    details = Column(String, nullable=True)
    hmac_signature = Column(String, nullable=False)


class ReflectionState(Base):
    # Wasserzeichen fuer die Post-Session-Reflektion: bis zu welchem
    # Timestamp wurde bereits reflektiert.
    __tablename__ = "reflection_state"
    user_id = Column(Integer, primary_key=True)
    reflected_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Mission(Base):
    # Cloud-Agenten-Mission (Setup4). Payload-Felder sind verschluesselt;
    # die PII-Platzhalter-Map verlaesst den Host nie.
    __tablename__ = "missions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=True)
    goal_encrypted = Column(LargeBinary, nullable=False)
    context_encrypted = Column(LargeBinary, nullable=True)
    pii_map_encrypted = Column(LargeBinary, nullable=True)
    provider = Column(String, nullable=False, default="gemini")
    status = Column(String, nullable=False, default="queued", index=True)
    current_step = Column(String, nullable=True)
    calls_used = Column(Integer, nullable=False, default=0)
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    result_encrypted = Column(LargeBinary, nullable=True)
    error = Column(String, nullable=True)
    # Herkunft fuer "Ergebnis zurueck zur Quelle": dashboard|api (alte Zeilen: telegram).
    origin = Column(String, nullable=False, default="api")
    owui_chat_id = Column(String, nullable=True)
    owui_message_id = Column(String, nullable=True)
    owui_user_id = Column(String, nullable=True)
    dashboard_session_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
