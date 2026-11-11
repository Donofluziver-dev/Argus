from sqlalchemy import inspect, text


MESSAGE_PERFORMANCE_COLUMNS = {
    "latency_ms": "INTEGER",
    "ttft_ms": "INTEGER",
    "tokens_per_sec": "FLOAT",
    # Werkzeugschritte. BYTEA/BLOB je nach Datenbank -- der Typ wird beim
    # Anlegen aus dem Dialekt bestimmt, siehe unten.
}
MESSAGE_BLOB_COLUMNS = ("steps_encrypted",)


def ensure_message_performance_columns(bind) -> None:
    inspector = inspect(bind)
    if "messages" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("messages")}
    missing = [
        (name, column_type)
        for name, column_type in MESSAGE_PERFORMANCE_COLUMNS.items()
        if name not in existing
    ]
    with bind.begin() as conn:
        for name, column_type in missing:
            conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {column_type}"))

    # Binaerspalten getrennt: der Typname unterscheidet sich je Datenbank
    # (PostgreSQL BYTEA, SQLite BLOB), ein fester Name schluege auf einer
    # der beiden fehl.
    blob = "BYTEA" if bind.dialect.name == "postgresql" else "BLOB"
    with bind.begin() as conn:
        for name in MESSAGE_BLOB_COLUMNS:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {blob}"))


def ensure_user_role_column(bind) -> None:
    '''users.role additiv nachziehen (Freigabe-Berechtigung).

    Bewusst hier statt als Alembic-Migration: die Revisionen 0007-0009 gehoeren
    zur Cloud-Schicht (Setup4). Eine Setup1-0010 muesste auf 0009 aufsetzen und
    waere fuer jeden Aufbau OHNE Setup4 eine zerrissene Kette. Idempotent, also
    gefahrlos bei jedem Start.'''
    inspector = inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "role" not in existing:
        #--- 'admin' gilt nur fuer Konten aus der Zeit vor den Rollen: Sie waren
        #--- allein auf ihrer Installation und duerfen sich nicht aussperren.
        with bind.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'admin'"))
    with bind.begin() as conn:
        #--- Die Rolle 'chat' heisst seit Version 2 'user'. Nur schreiben, wenn
        #--- solche Zeilen ueberhaupt vorkommen.
        if conn.execute(text("SELECT 1 FROM users WHERE role = 'chat' LIMIT 1")).first() is not None:
            conn.execute(text("UPDATE users SET role = 'user' WHERE role = 'chat'"))
        #--- Neue Zeilen ohne Angabe bekommen die kleinere Rolle. SQLite kann den
        #--- Standard einer Spalte nicht aendern; dort setzt das Modell ihn.
        if bind.dialect.name == "postgresql":
            cur_default = conn.execute(text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'role'"
            )).scalar()
            norm_default = str(cur_default or "").split("::")[0].strip().strip("'")
            if norm_default != "user":
                conn.execute(text("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'user'"))


def ensure_user_token_version_column(bind) -> None:
    '''users.token_version additiv nachziehen (Sitzungen widerrufen). Gleiches
    Muster wie ensure_user_role_column, idempotent.'''
    inspector = inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "token_version" in existing:
        return
    with bind.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"))


def ensure_sensitive_session_columns(bind) -> None:
    '''chat_sessions.has_sensitive_data idempotent nachziehen (Egress-Guard ueber
    Backend-Neustart hinweg).'''
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    with bind.begin() as conn:
        if "chat_sessions" in tables:
            cols = {column["name"] for column in inspector.get_columns("chat_sessions")}
            if "has_sensitive_data" not in cols:
                conn.execute(text(
                    "ALTER TABLE chat_sessions ADD COLUMN has_sensitive_data BOOLEAN NOT NULL DEFAULT FALSE"
                ))
