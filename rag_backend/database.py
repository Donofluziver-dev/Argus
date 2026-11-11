import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from rag_backend.crypto_utils import load_secret

db_user = os.getenv("POSTGRES_USER", "argus_user")
db_name = os.getenv("POSTGRES_DB", "ragdb")
db_pass = load_secret("POSTGRES_PASSWORD")
if db_user and db_pass:
    # Treiber ausdruecklich: SQLAlchemy 2.1 nimmt fuer "postgresql://" psycopg 3,
    # installiert ist psycopg2-binary.
    SQLALCHEMY_DATABASE_URL = f"postgresql+psycopg2://{db_user}:{db_pass}@postgres:5432/{db_name}"
else:
    SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if not SQLALCHEMY_DATABASE_URL:
    print("FEHLER: DATABASE_URL konnte nicht konstruiert werden.", file=sys.stderr)
    sys.exit(1)
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
