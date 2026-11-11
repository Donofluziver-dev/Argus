import asyncio
import logging
import hmac
import mimetypes
import os
import uuid
import threading
import json
import secrets
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from jwt import InvalidTokenError
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, Response, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from langchain_core.messages import AIMessage, HumanMessage
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from rag_backend.models import User, ChatSession, Message, AuditLog, IngestedDocument, Mission, Base
from rag_backend.database import engine, get_db, SessionLocal
from rag_backend.agent import agent_executor, startup_load_models, startup_mcp_and_graph, _TOOL_STATUS
from rag_backend.memory import run_reflection_cycle
from rag_backend.crypto_utils import encrypt_msg, decrypt_msg, load_secret
from rag_backend import metrics
from rag_backend.utils import get_env_first
from rag_backend.schema_utils import (ensure_message_performance_columns, ensure_user_role_column,
                                      ensure_user_token_version_column, ensure_sensitive_session_columns)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# httpx loggt auf INFO jede Request-URL, und manche APIs tragen Schluessel in der
# URL. Auf WARNING heben verhindert dieses Leck.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


#--- Zweite Linie hinter dem Level-Anheben: das greift nur, solange NIEMAND einen
#--- Schluessel auf WARNING oder hoeher schreibt. Eine Client-Exception traegt oft
#--- die aufgerufene URL im Text, und eine Bibliothek im Debug-Modus reicht
#--- ebenfalls. Der Filter haengt an den ROOT-HANDLERN und sieht damit jeden
#--- Datensatz, auch die aus fremden Loggern propagierten.
class _SecretRedactingFilter(logging.Filter):
    _MASK = "[REDACTED_SECRET]"

    def __init__(self, secrets: list):
        super().__init__()
        #--- Ein leerer oder sehr kurzer Wert ist kein Secret, sondern ein
        #--- Suchmuster, das die halben Logs schwaerzen wuerde.
        self._literals = [s for s in secrets if s and len(s) >= 12]

    def _redact(self, text: str) -> str:
        for secret in self._literals:
            text = text.replace(secret, self._MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = self._redact(message)
            if redacted != message:
                record.msg, record.args = redacted, ()
            #--- Tracebacks laufen an getMessage() vorbei. Vorformatieren und
            #--- ebenfalls saeubern -- der Formatter nimmt danach exc_text.
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = self._redact(record.exc_text)
        except Exception:
            pass  # Logging darf die Anwendung nie stoppen.
        return True


#--- Schluessel, die das Backend selbst nach aussen oder an Nachbardienste reicht.
_REDACTED_SECRETS = ("LLM_API_KEY", "GEMINI_API_KEY", "LANGCHAIN_API_KEY", "SERVICE_TOKEN",
                     "JWT_SECRET", "AUDIT_HMAC_KEY", "POSTGRES_PASSWORD")


def _install_secret_redaction() -> None:
    _secrets = []
    for name in _REDACTED_SECRETS:
        try:
            _secrets.append(load_secret(name))
        except Exception:
            pass
    _redactor = _SecretRedactingFilter(_secrets)
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_redactor)


_install_secret_redaction()


_ph = PasswordHasher()
JWT_SECRET = load_secret("JWT_SECRET")
#--- HS256 mit kurzem Schluessel ist offline knackbar; ein leerer Schluessel
#--- machte jedes Token gueltig. Lieber gar nicht starten.
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET fehlt oder ist kuerzer als 32 Zeichen.")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", 1))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Tool-Statuszeilen kommen als eigenes "status"-Feld, nicht ueber eine
# Emoji-Praefix-Erkennung -- die wuerde echte Antworten loeschen.

# ---- Schemas, Token- & Startup-Helpers #---

class RegisterRequest(BaseModel):
    email: str
    password: str

# Bild-Bloecke Caps: Anzahl pro Anfrage und Groesse pro data-URI.
_MAX_IMAGES = int(os.getenv("MAX_IMAGES_PER_REQUEST", 2))
_MAX_IMAGE_DATA_CHARS = int(os.getenv("MAX_IMAGE_DATA_CHARS", 10 * 1024 * 1024))

def _tv(user) -> int:
    return int(getattr(user, "token_version", 0) or 0)


def create_token(uid: int, tv: int = 0) -> str:
    payload = {
        "sub": str(uid),
        "tv": tv,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_service_user_if_not_exists(db: Session):
    from sqlalchemy import text as _sa_text
    if not db.get(User, 0):
        db.add(User(
            id=0,
            email="service-account@system.local",
            hashed_password="__service_account_no_login__",
            # Unter diesem Konto laufen Hintergrundjobs ohne angemeldete Person.
            role="admin",
        ))
        db.commit()
        try:
            db.execute(_sa_text(
                "SELECT setval(pg_get_serial_sequence('users', 'id'), GREATEST(1, (SELECT MAX(id) FROM users)))"
            ))
            db.commit()
        except Exception:
            pass

def _run_migrations():
    if os.getenv("SKIP_MIGRATIONS", "false").lower() == "true":
        return
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    from sqlalchemy import inspect as sa_inspect
    from rag_backend.database import engine

    cfg = AlembicConfig("/app/alembic.ini")

    with engine.connect() as conn:
        insp = sa_inspect(conn)
        table_names = insp.get_table_names()
        has_alembic = "alembic_version" in table_names
        has_users = "users" in table_names

    if has_users and not has_alembic:
        log.info("Bestehende DB ohne Alembic-History erkannt -- stemple auf Version 0001.")
        alembic_command.stamp(cfg, "0001")

    alembic_command.upgrade(cfg, "head")

    # Neue Modelle ohne eigene Alembic-Migration additiv anlegen. create_all
    # legt nur fehlende Tabellen an.
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as _e:
        log.warning(f"create_all (Zusatztabellen) fehlgeschlagen: {_e}")

    try:
        ensure_message_performance_columns(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for messages performance columns failed: {_e}")

    try:
        ensure_user_role_column(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for users.role failed: {_e}")

    try:
        ensure_user_token_version_column(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for users.token_version failed: {_e}")

    try:
        ensure_sensitive_session_columns(engine)
    except Exception as _e:
        log.warning(f"ALTER TABLE for sensitive session columns failed: {_e}")

def _ensure_qdrant_collection():
    import qdrant_client
    # Erstellungslogik lebt zentral in ingest.py. dim kommt hier aus der .env,
    # weil das Embedding-Modell beim Startup noch nicht geladen sein soll.
    from rag_backend.ingest import ensure_qdrant_collection
    url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection = os.getenv("QDRANT_COLLECTION", "docs")
    dim = int(os.getenv("EMBEDDING_DIM", 1024))
    try:
        qc = qdrant_client.QdrantClient(url=url)
        # Legt die Sammlung im Hybrid-Aufbau an oder zieht einen alten Aufbau
        # um (bettet dafuer ueber den Container embed neu ein).
        created = ensure_qdrant_collection(qc, collection, dim)
        if created:
            log.info(f"Qdrant Collection '{collection}' im Hybrid-Aufbau erstellt bzw. umgezogen (dim={dim}).")
        else:
            log.info(f"Qdrant Collection '{collection}' vorhanden.")
    except Exception as e:
        log.warning(f"Qdrant Collection konnte nicht erstellt werden: {e}")

async def _reflection_idle_watcher(app: FastAPI):
    # Post-Session-Reflektion: feuert nach REFLECTION_IDLE_SECONDS Leerlauf,
    # genau einmal pro Leerlaufphase.
    idle_threshold = int(os.getenv("REFLECTION_IDLE_SECONDS", "300"))
    check_interval = int(os.getenv("REFLECTION_CHECK_INTERVAL", "120"))
    last_marker = None
    while True:
        try:
            await asyncio.sleep(check_interval)
            last = getattr(app.state, "last_activity", None)
            if last is None or last == last_marker:
                continue
            idle = (datetime.now(timezone.utc) - last).total_seconds()
            if idle < idle_threshold:
                continue
            log.info(f"Leerlauf {int(idle)}s -> starte Post-Session-Reflektion.")
            await run_reflection_cycle()
            last_marker = last
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Reflection-Watcher: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.last_activity = None
    await asyncio.to_thread(_run_migrations)
    db = SessionLocal()
    try:
        create_service_user_if_not_exists(db)
        metrics.load_historical_metrics(db)
    finally:
        db.close()
    await asyncio.to_thread(_ensure_qdrant_collection)

    #--- MCP + Graph synchron vor yield, damit erste Requests ein komplettes
    #    Tool-Set sehen.
    app.state.mcp_started = False
    try:
        await startup_mcp_and_graph()
        app.state.mcp_started = True
    except Exception as e:
        log.warning(f"startup_mcp_and_graph fehlgeschlagen: {e}")

    #--- Embedding/Reranker/HTTP-Clients im Background.
    app.state.loader_task = asyncio.create_task(startup_load_models())

    app.state.reflection_task = asyncio.create_task(_reflection_idle_watcher(app))

    #--- Cloud-Agenten (Setup4): Missions-Loop. Import geschuetzt, damit das
    #    Backend auch ohne Setup4-Output bootet.
    app.state.mission_task = None
    if os.getenv("CLOUD_AGENTS_ENABLED", "true").lower() == "true":
        try:
            from rag_backend.cloud_agents.missions import mission_loop
            app.state.mission_task = asyncio.create_task(mission_loop(app))
        except ImportError as e:
            log.warning(f"Cloud-Agents nicht verfuegbar (Paket rag_backend.cloud_agents fehlt?): {e}")

    #--- Uploads-Ordner sauber halten (UPLOAD_RETENTION_HOURS, 0 = aus).
    app.state.uploads_task = asyncio.create_task(_uploads_sweeper())

    yield

    if getattr(app.state, "mcp_started", False):
        try:
            from rag_backend.mcp_loader import shutdown_mcp
            await shutdown_mcp()
        except Exception:
            pass
    task = getattr(app.state, "loader_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    rtask = getattr(app.state, "reflection_task", None)
    if rtask and not rtask.done():
        rtask.cancel()
        try:
            await rtask
        except (asyncio.CancelledError, Exception):
            pass
    mtask = getattr(app.state, "mission_task", None)
    if mtask and not mtask.done():
        mtask.cancel()
        try:
            await mtask
        except (asyncio.CancelledError, Exception):
            pass
    utask = getattr(app.state, "uploads_task", None)
    if utask and not utask.done():
        utask.cancel()
        try:
            await utask
        except (asyncio.CancelledError, Exception):
            pass

def _init_tracing() -> None:
    #--- Lokales Tracing nach Phoenix. FAIL-OPEN: laeuft der Container nicht,
    #--- startet das Backend trotzdem. Observability ist Beiwerk. Die Inhalte
    #--- werden vor dem Versand geschwaerzt (TRACE_CONTENT, trace_redaction.py).
    endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
    if not endpoint:
        log.info("Tracing: kein PHOENIX_COLLECTOR_ENDPOINT gesetzt -- deaktiviert.")
        return
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from rag_backend.trace_redaction import build_phoenix_tracer_provider, trace_mode
        mode = trace_mode()
        tracer_provider = build_phoenix_tracer_provider(
            endpoint, os.getenv("PHOENIX_PROJECT_NAME", "argus"), mode)
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider, skip_dep_check=True)
        log.info(f"Tracing aktiv: Phoenix unter {endpoint}, Inhalte: {mode}")
    except Exception as e:
        log.warning(f"Tracing konnte nicht gestartet werden ({type(e).__name__}: {e}) -- laeuft ohne.")


_init_tracing()

limiter = Limiter(key_func=get_remote_address)
#--- Die API-Beschreibung (/docs, /redoc, /openapi.json) ist ohne Anmeldung lesbar
#--- und zaehlt jede Route auf. Aus, solange sie niemand ausdruecklich einschaltet.
_API_DOCS = os.getenv("API_DOCS", "false").strip().lower() == "true"
app = FastAPI(
    title="Argus",
    lifespan=lifespan,
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

#--- Host-Header-Gate gegen DNS-Rebinding. Die lesenden Dashboard-Endpoints
#--- pruefen keinen Origin; die Bindung an 127.0.0.1 allein schuetzt sie nicht.
#--- Zuletzt hinzugefuegt = aeusserste Schicht, laeuft vor CORS und allen Routen.
#--- 'rag-backend' MUSS drinbleiben (Docker-Netzwerk / interner Service-Name),
#--- ebenso 'localhost' (Healthcheck). Der Port ist nicht Teil des Vergleichs.
_TRUSTED_HOSTS = [
    h.strip() for h in os.getenv(
        "TRUSTED_HOSTS", "127.0.0.1,localhost,rag-backend"
    ).split(",") if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_TRUSTED_HOSTS)

#--- Sicherheits-Header. Zweite Linie hinter escapeHtml: das Dashboard zeigt
#--- Text an, den ein Modell geschrieben hat. Rutscht etwas durch, verhindert
#--- connect-src/img-src wenigstens den Abfluss.
#--- 'unsafe-inline' bei Skripten ist noetig, solange Knoepfe onclick tragen.
#--- Kein fremder Host.
#--- Ablaufverfolgung (Phoenix) laeuft im eigenen Container und wird in der
#--- Monitor-Ansicht eingebettet. Die Adresse ist die, unter der der BROWSER
#--- Phoenix erreicht: auf dem eigenen Rechner 127.0.0.1:6006, auf einem Server
#--- dessen Adresse. Leer = Reiter aus, dann darf die Seite nichts einbetten.
def _clean_traces_url(raw: str) -> str:
    #--- Der Wert landet im CSP-Header: ein Semikolon oder Leerzeichen darin
    #--- wuerde weitere Direktiven einschleusen. Nur eine einfache http(s)-Adresse.
    url = (raw or "").strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~/\-]*)?", url):
        log.warning("DASHBOARD_TRACES_URL ist keine einfache http(s)-Adresse -- Ablaufverfolgung aus.")
        return ""
    return url


def _frame_src(traces_url: str) -> str:
    origin = "/".join(traces_url.split("/")[:3]) if traces_url else ""
    src = "'self'" + (f" {origin}" if origin else "")
    if origin == "http://127.0.0.1:6006":
        src += " http://localhost:6006"
    return src


_TRACES_URL = _clean_traces_url(os.getenv("DASHBOARD_TRACES_URL", "http://127.0.0.1:6006"))
_FRAME_SRC = _frame_src(_TRACES_URL)
_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    f"frame-src {_FRAME_SRC}; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# ---- Browser-Oberflaeche (statische Seite, same-origin unter /dashboard/) #---
class _DashboardStatics(StaticFiles):
    '''Liefert rag_backend/static aus und laesst den Browser jede Datei pruefen.

    Die Dateinamen bleiben nach einem Update gleich. Ohne "no-cache" zeigte der
    Browser nach einem Neubau weiter die alte Oberflaeche, bis jemand Strg+F5
    drueckt. Die Pruefung kostet je Datei eine kurze Anfrage (ETag, Antwort 304).'''

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


#--- Ohne diese Zeile liefert StaticFiles die Schriften als
#--- application/octet-stream aus.
mimetypes.add_type("font/woff2", ".woff2")

_DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", str(Path(__file__).resolve().parent / "static"))
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/dashboard", _DashboardStatics(directory=_DASHBOARD_DIR, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    async def root_to_dashboard():
        return RedirectResponse("/dashboard/", status_code=307)

# ---- Output-Bereinigung, Auth & CSRF #---

# Status-Emojis direkt aus den _TOOL_STATUS-Labels abgeleitet statt aus einer
# zweiten, driftfaehigen Liste. Dazu die Zeilen ohne _TOOL_STATUS-Eintrag.
_STATUS_EMOJIS = tuple({fn({})[0] for fn in _TOOL_STATUS.values()}) + (
    "⏳", "\U0001F9E0", "\U0001F4DD", "\U0001F527",
)


def strip_status_lines(text: str | None) -> str:
    if not text:
        return ""
    # Vergleich auf Basis-Codepoints OHNE Variation Selector (U+FE0F).
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip().replace("️", "")
        if s and any(s.startswith(emoji) for emoji in _STATUS_EMOJIS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        sub = payload["sub"]
        if sub == "service":
            uid = 0
        else:
            uid = int(sub)
            if uid == 0:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungültiges Token.")
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltiges oder abgelaufenes Token.")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Benutzer nicht gefunden.")
    if sub != "service" and int(payload.get("tv", 0)) != _tv(user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token widerrufen.")
    return user

def check_csrf_headers(request: Request):
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    allowed = [o.lower() for o in _CORS_ORIGINS]
    
    if origin:
        if origin.lower() not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Origin nicht erlaubt.")
        return
        
    if referer:
        try:
            from urllib.parse import urlparse
            ref_parsed = urlparse(referer)
            ref_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}"
            if ref_origin.lower() not in allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Referer nicht erlaubt.")
        except Exception:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF-Schutz: Ungueltiger Referer.")


#--- Dashboard-CSRF: strikt Same-Origin. Der Cookie ist SameSite=Lax -- ohne
#    dieses Gate waere jede schreibende Aktion per fremder Seite ausloesbar.
_DASHBOARD_ORIGINS = {
    o.strip().lower()
    for o in os.getenv(
        "DASHBOARD_ORIGINS",
        "http://127.0.0.1:7860,http://localhost:7860",
    ).split(",")
    if o.strip()
}


def check_dashboard_csrf(request: Request):
    from urllib.parse import urlparse

    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if referer:
            try:
                p = urlparse(referer)
                origin = f"{p.scheme}://{p.netloc}"
            except Exception:
                origin = None
    #--- Fehlender Origin UND Referer = kein Browser-Kontext. Bewusst abgelehnt.
    if not origin or origin.lower() not in _DASHBOARD_ORIGINS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="CSRF-Schutz: schreibende Dashboard-Aktionen nur same-origin.",
        )


# ---- Dashboard-Anmeldung: Konten, Rollen, Sitzungs-Cookie #---
#--- Zwei Rollen: 'admin' verwaltet, 'user' fragt, sucht und recherchiert. Jede
#--- Pruefung sitzt SERVERSEITIG an der Route.
DASHBOARD_ROLES = ("admin", "user")
_DASHBOARD_COOKIE = os.getenv("DASHBOARD_COOKIE_NAME", "argus_session")
_DASHBOARD_SESSION_HOURS = int(os.getenv("DASHBOARD_SESSION_HOURS", "12"))
#--- 'Angemeldet bleiben'. Die Laufzeit steckt IM Token, nicht nur im Cookie.
_DASHBOARD_REMEMBER_DAYS = int(os.getenv("DASHBOARD_REMEMBER_DAYS", "7"))
#--- id=0 ist das Dienstkonto. Es hat kein anmeldbares Passwort und taucht in
#--- keiner Benutzerliste auf.
_SERVICE_UID = 0


class DashboardLoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False


class DashboardUserIn(BaseModel):
    username: str
    password: str
    role: str = "user"


class DashboardPasswordIn(BaseModel):
    password: str
    #--- Beim eigenen Passwortwechsel Pflicht: ein offenes Fenster oder ein
    #--- gestohlenes Cookie allein reicht dann nicht, um das Konto zu uebernehmen.
    current_password: str = ""


def _dashboard_session_token(uid: int, seconds: int, tv: int = 0) -> str:
    #--- Eigener scope-Anspruch: ein Dashboard-Cookie ist KEIN API-Token. Ohne
    #--- die Trennung liesse sich der Service-Token als Sitzungs-Cookie setzen.
    payload = {
        "sub": str(uid),
        "scope": "dashboard",
        "tv": tv,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=seconds),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _ist_https(request: Request) -> bool:
    #--- Hinter einem Tunnel (Cloudflare, Reverse Proxy) kommt die Anfrage
    #--- intern per HTTP an; das echte Schema steht in X-Forwarded-Proto.
    fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return fwd == "https" or request.url.scheme == "https"


def _set_session_cookie(response: Response, uid: int, remember: bool = False,
                        request: Request | None = None, tv: int = 0) -> None:
    seconds = (_DASHBOARD_REMEMBER_DAYS * 86400 if remember
               else _DASHBOARD_SESSION_HOURS * 3600)
    #--- Secure nur ueber HTTPS: fest auf True wuerde der Browser das Cookie beim
    #--- lokalen Zugang ueber http://127.0.0.1 verwerfen -- die Anmeldung liefe
    #--- dann ins Leere, ohne Fehlermeldung.
    response.set_cookie(
        _DASHBOARD_COOKIE,
        _dashboard_session_token(uid, seconds, tv),
        max_age=seconds,
        httponly=True,
        secure=bool(request is not None and _ist_https(request)),
        samesite="lax",
        path="/",
    )


def _login_accounts(db: Session):
    return db.query(User).filter(User.id != _SERVICE_UID).order_by(User.id.asc()).all()


def dashboard_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(_DASHBOARD_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Nicht angemeldet.")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("scope") != "dashboard":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltige Sitzung.")
        uid = int(payload["sub"])
    except HTTPException:
        raise
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sitzung abgelaufen.")
    if uid == _SERVICE_UID:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Ungueltige Sitzung.")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Konto nicht mehr vorhanden.")
    if int(payload.get("tv", 0)) != _tv(user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sitzung beendet.")
    return user


def dashboard_admin(user: User = Depends(dashboard_user)) -> User:
    if getattr(user, "role", "user") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Dieser Bereich ist Administratoren vorbehalten.")
    return user


def _register_allowed() -> bool:
    #--- Fail-closed wie ENABLE_BACKEND_SIGNUP: alles ausser exakt "true" sperrt,
    #--- auch eine fehlende Variable.
    return os.getenv("DASHBOARD_ALLOW_REGISTER", "false").strip().lower() == "true"


@app.get("/v1/dashboard/auth/state", tags=["Dashboard"])
def dashboard_auth_state(request: Request, db: Session = Depends(get_db)):
    #--- Erstlauf: solange kein Konto existiert, bietet die Oberflaeche das
    #--- Anlegen des ersten Administrators an.
    accounts = _login_accounts(db)
    out = {"needs_setup": not accounts, "authenticated": False,
           "allow_register": bool(accounts) and _register_allowed()}
    if not accounts:
        #--- Nur OB ein Einrichtungs-Schluessel verlangt wird, nie sein Wert: die
        #--- Oberflaeche zeigt dann ein Feld dafuer.
        out["setup_token_required"] = bool(os.getenv("DASHBOARD_SETUP_TOKEN", "").strip())
    try:
        user = dashboard_user(request, db)
    except HTTPException:
        return out
    out.update(authenticated=True, username=user.email,
               role=getattr(user, "role", "user"), user_id=user.id)
    return out


@app.post("/v1/dashboard/auth/register", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_REGISTER_RATE_LIMIT", "5/minute"))
def dashboard_auth_register(request: Request, body: DashboardLoginIn,
                            db: Session = Depends(get_db),
                            csrf = Depends(check_dashboard_csrf)):
    '''Selbstregistrierung. Das angelegte Konto bekommt IMMER die Rolle 'user'.

    Die Rolle steht hier hart im Code und kommt nicht aus dem Body -- sonst koennte
    sich jeder mit einem zusaetzlichen Feld zum Administrator machen. Aus demselben
    Grund taugt der alte /register-Endpunkt nicht als Grundlage: er setzt gar keine
    Rolle, und der Spalten-Default ist "admin".'''
    if not _register_allowed():
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Selbstregistrierung ist abgeschaltet.")
    if not _login_accounts(db):
        #--- Vor dem ersten Konto fuehrt der Weg ueber /auth/setup.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Zuerst muss das Administrator-Konto angelegt werden.")
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    if db.query(User).filter_by(email=name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Benutzername ist vergeben.")
    user = User(email=name, hashed_password=_ph.hash(body.password), role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info(f"Selbstregistrierung: '{user.email}' (Rolle user, id={user.id})")
    resp = JSONResponse({"status": "ok", "username": user.email, "role": user.role,
                         "user_id": user.id})
    _set_session_cookie(resp, user.id, remember=body.remember, request=request, tv=_tv(user))
    return resp


_SETUP_LOCK = threading.Lock()


@app.post("/v1/dashboard/auth/setup", tags=["Dashboard"])
@limiter.limit("5/minute")
def dashboard_auth_setup(request: Request, body: DashboardUserIn,
                         db: Session = Depends(get_db),
                         csrf = Depends(check_dashboard_csrf)):
    #--- Optionaler Einrichtungs-Schluessel: ist DASHBOARD_SETUP_TOKEN gesetzt,
    #--- kann nur, wer ihn kennt, den ersten Administrator anlegen -- nicht ein
    #--- beliebiger Prozess, der den Erstlauf zuerst erwischt.
    setup_token = os.getenv("DASHBOARD_SETUP_TOKEN", "").strip()
    if setup_token and not hmac.compare_digest(
            (request.headers.get("x-setup-token") or "").encode(), setup_token.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Einrichtungs-Schlüssel fehlt oder stimmt nicht.")
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    #--- Pruefen und Anlegen unter einer Sperre: zwei gleichzeitige Erstlaeufe
    #--- legten sonst zwei Administratoren an.
    with _SETUP_LOCK:
        if _login_accounts(db):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="Es existiert bereits ein Konto -- bitte anmelden.")
        user = User(email=name, hashed_password=_ph.hash(body.password), role="admin")
        db.add(user)
        db.commit()
        db.refresh(user)
    #--- Bestehende Verlaeufe lagen unter dem Dienstkonto und gehen an den ersten
    #--- Administrator ueber, statt unerreichbar zu werden.
    db.query(ChatSession).filter_by(user_id=_SERVICE_UID).update({"user_id": user.id})
    db.commit()
    resp = JSONResponse({"status": "ok", "username": user.email, "role": user.role,
                         "user_id": user.id})
    _set_session_cookie(resp, user.id, request=request)
    return resp


#--- Fehlversuche pro Benutzername. Das IP-Limit allein greift hinter der
#--- Docker-Portweiterleitung nicht: alle Clients kommen von derselben Adresse.
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_FAIL_LOCK = threading.Lock()
_LOGIN_MAX_FAILS = int(os.getenv("DASHBOARD_LOGIN_MAX_FAILS", "5"))
_LOGIN_LOCK_SECONDS = int(os.getenv("DASHBOARD_LOGIN_LOCK_SECONDS", "900"))
#--- Gegen Timing: unbekannte Namen pruefen trotzdem einen Hash.
_DUMMY_HASH = _ph.hash("argus-dummy-password-for-timing")


def _login_locked(name: str) -> bool:
    now = time.time()
    with _LOGIN_FAIL_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(name, []) if now - t < _LOGIN_LOCK_SECONDS]
        _LOGIN_FAILS[name] = fails
        return len(fails) >= _LOGIN_MAX_FAILS


def _login_failed(name: str) -> None:
    with _LOGIN_FAIL_LOCK:
        _LOGIN_FAILS.setdefault(name, []).append(time.time())
        if len(_LOGIN_FAILS) > 10000:
            _LOGIN_FAILS.clear()


@app.post("/v1/dashboard/auth/login", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_LOGIN_RATE_LIMIT", "10/minute"))
def dashboard_auth_login(request: Request, body: DashboardLoginIn,
                         db: Session = Depends(get_db),
                         csrf = Depends(check_dashboard_csrf)):
    name = body.username.strip()
    if _login_locked(name.lower()):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Zu viele Fehlversuche. Bitte spaeter erneut versuchen.")
    user = db.query(User).filter_by(email=name).first()
    #--- Gleiche Meldung UND gleiche Laufzeit fuer 'Konto gibt es nicht' und
    #--- 'Passwort falsch'.
    try:
        if not user or user.id == _SERVICE_UID:
            _ph.verify(_DUMMY_HASH, body.password + "\x00")
            raise VerifyMismatchError()
        _ph.verify(user.hashed_password, body.password)
    except (VerifyMismatchError, InvalidHash):
        _login_failed(name.lower())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Anmeldung fehlgeschlagen.")
    if _ph.check_needs_rehash(user.hashed_password):
        user.hashed_password = _ph.hash(body.password)
        db.commit()
    resp = JSONResponse({"status": "ok", "username": user.email,
                         "role": getattr(user, "role", "user"), "user_id": user.id,
                         "remember_days": _DASHBOARD_REMEMBER_DAYS if body.remember else 0})
    _set_session_cookie(resp, user.id, remember=body.remember, request=request, tv=_tv(user))
    return resp


@app.post("/v1/dashboard/auth/logout", tags=["Dashboard"])
def dashboard_auth_logout(request: Request, db: Session = Depends(get_db),
                          csrf = Depends(check_dashboard_csrf)):
    #--- Serverseitig widerrufen: ein kopiertes Cookie gilt danach nicht mehr.
    try:
        user = dashboard_user(request, db)
        user.token_version = _tv(user) + 1
        db.commit()
    except HTTPException:
        pass
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(_DASHBOARD_COOKIE, path="/")
    return resp


@app.post("/v1/dashboard/auth/password", tags=["Dashboard"])
def dashboard_auth_password(request: Request, body: DashboardPasswordIn,
                            user: User = Depends(dashboard_user),
                            db: Session = Depends(get_db),
                            csrf = Depends(check_dashboard_csrf)):
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passwort ab 8 Zeichen.")
    try:
        _ph.verify(user.hashed_password, body.current_password)
    except (VerifyMismatchError, InvalidHash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Aktuelles Passwort stimmt nicht.")
    user.hashed_password = _ph.hash(body.password)
    #--- Alle anderen Sitzungen enden; diese bekommt ein frisches Cookie.
    user.token_version = _tv(user) + 1
    db.commit()
    resp = JSONResponse({"status": "ok"})
    _set_session_cookie(resp, user.id, request=request, tv=_tv(user))
    return resp


@app.get("/v1/dashboard/users", tags=["Dashboard"])
def dashboard_users(db: Session = Depends(get_db), admin: User = Depends(dashboard_admin)):
    from sqlalchemy import func

    #--- Zahlen je Konto in EINER Abfrage statt einer pro Konto.
    counts = dict(
        db.query(ChatSession.user_id, func.count(ChatSession.id))
        .group_by(ChatSession.user_id).all()
    )
    msg_rows = (db.query(ChatSession.user_id,
                         func.count(Message.id),
                         func.max(Message.timestamp))
                .join(Message, Message.session_id == ChatSession.id)
                .group_by(ChatSession.user_id).all())
    msgs = {uid: (n, last) for uid, n, last in msg_rows}

    out = []
    for u in _login_accounts(db):
        n_msgs, last = msgs.get(u.id, (0, None))
        out.append({
            "id": u.id,
            "username": u.email,
            "role": getattr(u, "role", "user"),
            "chats": counts.get(u.id, 0),
            "messages": n_msgs,
            "last_active": last.isoformat() if last else None,
            "is_self": u.id == admin.id,
        })
    return {"users": out}


@app.post("/v1/dashboard/users", tags=["Dashboard"])
def dashboard_user_create(body: DashboardUserIn, db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin),
                          csrf = Depends(check_dashboard_csrf)):
    name = body.username.strip()
    if len(name) < 3 or len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Benutzername ab 3, Passwort ab 8 Zeichen.")
    if body.role not in DASHBOARD_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekannte Rolle.")
    if db.query(User).filter_by(email=name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Benutzername ist vergeben.")
    user = User(email=name, hashed_password=_ph.hash(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.email, "role": user.role, "chats": 0}


@app.post("/v1/dashboard/users/{user_id}/password", tags=["Dashboard"])
def dashboard_user_password(user_id: int, body: DashboardPasswordIn,
                            db: Session = Depends(get_db),
                            admin: User = Depends(dashboard_admin),
                            csrf = Depends(check_dashboard_csrf)):
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Passwort ab 8 Zeichen.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    target.hashed_password = _ph.hash(body.password)
    target.token_version = _tv(target) + 1
    db.commit()
    return {"status": "ok"}


@app.post("/v1/dashboard/users/{user_id}/role", tags=["Dashboard"])
def dashboard_user_role(user_id: int, role: str, db: Session = Depends(get_db),
                        admin: User = Depends(dashboard_admin),
                        csrf = Depends(check_dashboard_csrf)):
    if role not in DASHBOARD_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unbekannte Rolle.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    #--- Der letzte Administrator darf sich nicht selbst degradieren.
    if target.role == "admin" and role != "admin":
        remaining = db.query(User).filter(User.role == "admin",
                                          User.id != user_id, User.id != _SERVICE_UID).count()
        if not remaining:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="Das ist der letzte Administrator.")
    target.role = role
    #--- Bestehende Sitzungen des Kontos beenden, damit die neue Rolle sofort gilt.
    target.token_version = _tv(target) + 1
    db.commit()
    return {"status": "ok", "id": target.id, "role": target.role}


@app.delete("/v1/dashboard/users/{user_id}", tags=["Dashboard"])
def dashboard_user_delete(user_id: int, db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin),
                          csrf = Depends(check_dashboard_csrf)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Das eigene Konto laesst sich nicht loeschen.")
    target = db.get(User, user_id)
    if not target or target.id == _SERVICE_UID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Konto nicht gefunden.")
    #--- Chats des Kontos gehen mit, sonst bleiben verschluesselte Verlaeufe
    #--- ohne Besitzer stehen.
    sessions = db.query(ChatSession).filter_by(user_id=user_id).all()
    for s in sessions:
        db.query(Message).filter_by(session_id=s.id).delete()
        db.delete(s)
    #--- Gedaechtnis des Kontos geht mit: das Wasserzeichen in der Datenbank und der
    #--- Ordner users/<id>. Sonst blieben Notizen ueber die Person liegen.
    from rag_backend.models import ReflectionState
    db.query(ReflectionState).filter_by(user_id=user_id).delete()
    db.delete(target)
    db.commit()
    _remove_user_files(user_id)
    return {"status": "deleted", "id": user_id, "chats_removed": len(sessions)}


def _remove_user_files(user_id: int) -> None:
    import shutil
    base = (Path(os.getenv("ARGUS_WORKSPACE", "/host/argus_workspace")) / "users").resolve(strict=False)
    target = (base / str(int(user_id))).resolve(strict=False)
    #--- Nur ein direkter Unterordner von users/, nie etwas anderes.
    if target.parent != base:
        return
    shutil.rmtree(target, ignore_errors=True)


@app.post("/register")
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    if os.getenv("ENABLE_BACKEND_SIGNUP", "false").lower() != "true":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Registrierung deaktiviert.")
    if db.query(User).filter_by(email=body.email).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Email existiert bereits.")
    #--- Rolle ausdruecklich: der Spalten-Default ist 'admin' (Altbestand).
    db.add(User(email=body.email, hashed_password=_ph.hash(body.password), role="user"))
    db.commit()
    return {"email": body.email}

@app.post("/login")
@limiter.limit("5/minute")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=form.username).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Falsche Anmeldedaten.")
    try:
        _ph.verify(user.hashed_password, form.password)
    except (VerifyMismatchError, InvalidHash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Falsche Anmeldedaten.")
    #--- API-Tokens erreichen Missionen und Freigaben -- nur fuer Administratoren.
    if getattr(user, "role", "user") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API-Zugang nur fuer Administratoren.")
    if _ph.check_needs_rehash(user.hashed_password):
        user.hashed_password = _ph.hash(form.password)
        db.commit()
    return {"access_token": create_token(user.id, _tv(user)), "token_type": "bearer"}

@app.get("/healthz", tags=["System"])
async def health():
    return {"status": "ok"}

# ---- Confirmation Endpoints (Action-Engine Phase 2) #---
#--- Jeder Endpoint reicht die User-ID des Aufrufers als actor durch. Nur wer
#--- die Aktion ausgeloest hat, darf sie ueber HTTP sehen oder freigeben.

@app.get("/v1/confirmations/pending", tags=["Action-Engine"])
async def list_pending_confirmations(current_user: User = Depends(get_current_user)):
    from rag_backend.action_engine.confirmation import confirmation_store
    pending = confirmation_store.list_pending(actor=current_user.id)
    return {"confirmations": [r.model_dump() for r in pending]}

def _require_admin(user: User) -> None:
    '''Nur Administratoren duerfen Host-Aktionen freigeben.

    Heute hat jedes Konto die Rolle "admin" (Einzelnutzer-Betrieb) -- der Check ist
    das Fundament fuer den Fall, dass weitere Nutzer dazukommen: die duerfen den
    Assistenten dann benutzen, aber keine schreibenden Aktionen auf dem Rechner
    freigeben. Lesen der eigenen offenen Anfragen bleibt erlaubt.'''
    if getattr(user, "role", "user") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Nur Administratoren duerfen Host-Aktionen freigeben.",
        )

@app.post("/v1/confirmations/{confirmation_id}/approve", tags=["Action-Engine"])
async def approve_confirmation(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
    _require_admin(current_user)
    req = confirmation_store.approve(confirmation_id, actor=current_user.id)
    if not req:
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    if req.status == ConfirmationStatus.COOLDOWN:
        return {"status": "cooldown", "cooldown_seconds": confirmation_store._cooldown, "message": "Cool-Down aktiv. Erneut bestaetigen nach Ablauf."}
    return {"status": req.status.value}

@app.post("/v1/confirmations/{confirmation_id}/confirm-cooldown", tags=["Action-Engine"])
async def confirm_after_cooldown(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store
    _require_admin(current_user)
    req, remaining = confirmation_store.confirm_after_cooldown(confirmation_id, actor=current_user.id)
    if not req:
        if remaining > 0:
            return {"status": "cooldown", "seconds_remaining": round(remaining, 1)}
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    return {"status": req.status.value}

@app.post("/v1/confirmations/{confirmation_id}/reject", tags=["Action-Engine"])
async def reject_confirmation(confirmation_id: str, current_user: User = Depends(get_current_user), csrf = Depends(check_csrf_headers)):
    from rag_backend.action_engine.confirmation import confirmation_store
    _require_admin(current_user)
    req = confirmation_store.reject(confirmation_id, actor=current_user.id)
    if not req:
        raise HTTPException(status_code=404, detail="Confirmation nicht gefunden.")
    return {"status": req.status.value}

# ---- Mission-Endpoints (Cloud-Agenten, Setup4) #---

def _mission_to_dict(m: Mission, include_result: bool) -> dict:
    from rag_backend.crypto_utils import decrypt_msg
    d = {
        "id": m.id, "status": m.status, "provider": m.provider,
        "current_step": m.current_step, "calls_used": m.calls_used,
        "tokens_in": m.tokens_in, "tokens_out": m.tokens_out, "error": m.error,
        "origin": getattr(m, "origin", None),
        "goal": decrypt_msg(m.goal_encrypted),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if include_result:
        d["result"] = decrypt_msg(m.result_encrypted) if m.result_encrypted else None
    return d

def _mission_admin(current_user: User = Depends(get_current_user)) -> User:
    #--- Missionen enthalten entschluesselte Ziele und Ergebnisse aller Konten.
    if getattr(current_user, "role", "user") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Nur fuer Administratoren.")
    return current_user


@app.get("/v1/missions", tags=["Cloud-Agents"])
async def list_missions(current_user: User = Depends(_mission_admin), db: Session = Depends(get_db)):
    rows = db.query(Mission).order_by(Mission.created_at.desc()).limit(50).all()
    return {"missions": [_mission_to_dict(r, include_result=False) for r in rows]}

@app.get("/v1/missions/{mission_id}", tags=["Cloud-Agents"])
async def get_mission(mission_id: int, current_user: User = Depends(_mission_admin), db: Session = Depends(get_db)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    return _mission_to_dict(row, include_result=True)

@app.post("/v1/missions/{mission_id}/cancel", tags=["Cloud-Agents"])
async def cancel_mission_endpoint(mission_id: int, current_user: User = Depends(_mission_admin), csrf = Depends(check_csrf_headers)):
    from rag_backend.cloud_agents.missions import request_cancel
    ok = await asyncio.to_thread(request_cancel, mission_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden oder bereits beendet.")
    return {"status": "cancelling"}

# ---- Dashboard-Endpoints #---
#--- Jede Route traegt ihr Gate im Signatur-Kopf: Depends(dashboard_user) =
#--- jedes angemeldete Konto, Depends(dashboard_admin) = nur Administratoren.


def _dashboard_config() -> dict:
    return {
        "embedding_model": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        "rag_k": int(os.getenv("K_RERANKED", "5")),
        "web_timeout_ms": int(float(os.getenv("WEB_SEARCH_TIMEOUT", "25.0")) * 1000),
        #--- Steuert, ob der Argus-Chat den Anhang-Knopf anbietet. Im reinen
        #--- Textbetrieb saehe die Antwort sonst so aus, als haette die Engine das
        #--- Bild angesehen.
        "multimodal": get_env_first(
            "LLM_ENABLE_MULTIMODAL", "SGLANG_ENABLE_MULTIMODAL", default="false"
        ).lower() == "true",
        "max_images": _MAX_IMAGES,
        #--- Fuer die Fusszeile des Anhang-Dialogs: womit indexiert wird, soll man
        #--- sehen, BEVOR man auf Ingestieren drueckt.
        "chunk_size": int(os.getenv("CHUNK_SIZE", "1536")),
        "chunk_overlap": int(os.getenv("CHUNK_OVERLAP", "192")),
        "max_upload_mb": _MAX_UPLOAD_BYTES // (1024 * 1024),
        #--- Adresse der Ablaufverfolgung fuer den Monitor-Reiter, leer = aus.
        "traces_url": _TRACES_URL,
    }


@app.get("/v1/dashboard/config", tags=["Dashboard"])
async def dashboard_config(user: User = Depends(dashboard_user)):
    #--- Schlanke Variante der Kennzahlen fuer die Chat-Ansicht.
    return {"model": os.getenv("OAI_MODEL"), "config": _dashboard_config()}


@app.get("/v1/dashboard/summary", tags=["Dashboard"])
async def dashboard_summary(admin: User = Depends(dashboard_admin)):
    snap = metrics.snapshot()
    engine_metrics = await metrics.llm_metrics()
    tts_metrics_data = await metrics.tts_metrics()
    searxng_up = await metrics.searxng_online()
    #--- Der reale KV-Pool. _serving_budget() fragt ihn einmal beim LLM-Server ab und
    #--- cacht ihn; ohne diese Zahl ist der Prozentwert im Dashboard bezugslos --
    #--- 34% von 25000 ist etwas anderes als 34% von 7332, und welcher Pool es
    #--- wurde, entscheidet sich bei jedem Start neu.
    try:
        from rag_backend.agent import _serving_budget
        engine_metrics["kv_pool_tokens"] = _serving_budget()
    except Exception as e:
        log.debug(f"KV-Poolgroesse nicht ermittelbar: {e}")
    return {"model": os.getenv("OAI_MODEL"), "backend": snap, "engine": engine_metrics,
            "tts": tts_metrics_data, "searxng_online": searxng_up,
            "config": _dashboard_config()}

@app.get("/v1/dashboard/missions", tags=["Dashboard"])
async def dashboard_missions(db: Session = Depends(get_db),
                             admin: User = Depends(dashboard_admin)):
    rows = db.query(Mission).order_by(Mission.id.desc()).limit(20).all()
    return {"missions": [_mission_to_dict(r, include_result=False) for r in rows]}

@app.get("/v1/dashboard/missions/{mission_id}", tags=["Dashboard"])
async def dashboard_mission_detail(mission_id: int, db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    return _mission_to_dict(row, include_result=True)

#--- Missionen aufraeumen, bewusst nur BEENDETE: an einer laufenden arbeitet der
#--- mission_loop gerade. Zum Stoppen gibt es /cancel.
_MISSION_TERMINAL = ("done", "failed", "cancelled")


@app.delete("/v1/dashboard/missions", tags=["Dashboard"])
async def dashboard_clear_missions(db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin),
                                   csrf = Depends(check_dashboard_csrf)):
    rows = db.query(Mission).filter(Mission.status.in_(_MISSION_TERMINAL)).all()
    count = len(rows)
    for r in rows:
        db.delete(r)
    db.commit()
    aktiv = db.query(Mission).filter(Mission.status.notin_(_MISSION_TERMINAL)).count()
    msg = f"{count} beendete Mission(en) gelöscht."
    if aktiv:
        msg += f" {aktiv} laufende bleiben (erst abbrechen, dann löschen)."
    return {"status": "ok", "deleted": count, "active_kept": aktiv, "message": msg}


@app.delete("/v1/dashboard/missions/{mission_id}", tags=["Dashboard"])
async def dashboard_delete_mission(mission_id: int, db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin),
                                   csrf = Depends(check_dashboard_csrf)):
    row = db.get(Mission, mission_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mission nicht gefunden.")
    if row.status not in _MISSION_TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"Mission {mission_id} läuft noch (Status: {row.status}). Erst abbrechen, dann löschen.",
        )
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": mission_id}


@app.get("/v1/dashboard/audit", tags=["Dashboard"])
async def dashboard_audit(db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin)):
    #--- 'details' enthaelt die ausgefuehrte PowerShell im Klartext -- deshalb ist
    #--- diese Route Administratoren vorbehalten.
    rows = db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(50).all()
    return {"audit": [
        {"ts": r.ts.isoformat() if r.ts else None, "tool": r.tool, "tool_class": r.tool_class,
         "status": r.status, "latency_ms": r.latency_ms, "details": r.details} for r in rows
    ]}

@app.get("/v1/dashboard/audit/verify", tags=["Dashboard"])
async def dashboard_audit_verify(admin: User = Depends(dashboard_admin)):
    # Prueft die HMAC-Hash-Chain des Audit-Logs auf Manipulation/Luecken.
    from rag_backend.action_engine.audit import verify_audit_chain
    return verify_audit_chain()

@app.delete("/v1/dashboard/audit", tags=["Dashboard"])
async def dashboard_clear_audit(db: Session = Depends(get_db),
                                admin: User = Depends(dashboard_admin),
                                csrf = Depends(check_dashboard_csrf)):
    try:
        count = db.query(AuditLog).count()
        db.query(AuditLog).delete()
        db.commit()
    except Exception:
        log.exception("Audit-Log leeren fehlgeschlagen")
        raise HTTPException(status_code=500, detail="Audit-Log konnte nicht geleert werden.")
    #--- Das Leeren ist selbst ein Audit-Ereignis: der neue Kettenanfang zeigt,
    #--- wer wann wie viele Eintraege entfernt hat.
    try:
        from rag_backend.action_engine.audit import write_audit_log
        write_audit_log(tool="audit_log", tool_class="destructive", input_hash="",
                        output_hash=None, status="cleared", user_id=admin.id,
                        details=f"{count} Eintraege geloescht")
    except Exception:
        log.exception("Audit-Eintrag fuer das Leeren fehlgeschlagen")
    return {"status": "ok", "message": f"{count} Audit-Einträge aus der DB gelöscht. Dateien in Action_Audit/ bleiben erhalten."}

#--- Freigaben schreibender Host-Aktionen: Administratoren vorbehalten. actor =
#--- die User-ID des Kontos, unter dem der Turn lief; fremde Anfragen bleiben zu.
@app.get("/v1/dashboard/confirmations", tags=["Dashboard"])
async def dashboard_confirmations(admin: User = Depends(dashboard_admin)):
    from rag_backend.action_engine.confirmation import confirmation_store
    pending = confirmation_store.list_pending(actor=admin.id)
    return {"confirmations": [r.model_dump() for r in pending]}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/approve", tags=["Dashboard"])
async def dashboard_confirmation_approve(confirmation_id: str,
                                         admin: User = Depends(dashboard_admin),
                                         csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store, ConfirmationStatus
    req = confirmation_store.approve(confirmation_id, actor=admin.id, via="chat")
    if not req:
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    if req.status == ConfirmationStatus.COOLDOWN:
        return {"status": "cooldown", "cooldown_seconds": confirmation_store._cooldown,
                "message": "Cool-Down aktiv. Erneut bestaetigen nach Ablauf."}
    return {"status": req.status.value}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/confirm-cooldown", tags=["Dashboard"])
async def dashboard_confirmation_cooldown(confirmation_id: str,
                                          admin: User = Depends(dashboard_admin),
                                          csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store
    req, remaining = confirmation_store.confirm_after_cooldown(
        confirmation_id, actor=admin.id, via="chat")
    if not req:
        if remaining > 0:
            return {"status": "cooldown", "seconds_remaining": round(remaining, 1)}
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    return {"status": req.status.value}


@app.post("/v1/dashboard/confirmations/{confirmation_id}/reject", tags=["Dashboard"])
async def dashboard_confirmation_reject(confirmation_id: str,
                                        admin: User = Depends(dashboard_admin),
                                        csrf = Depends(check_dashboard_csrf)):
    from rag_backend.action_engine.confirmation import confirmation_store
    req = confirmation_store.reject(confirmation_id, actor=admin.id, via="chat")
    if not req:
        raise HTTPException(status_code=404, detail="Freigabe-Anfrage nicht gefunden.")
    return {"status": req.status.value}


@app.get("/v1/dashboard/state", tags=["Dashboard"])
async def dashboard_state(db: Session = Depends(get_db),
                          admin: User = Depends(dashboard_admin)):
    return {"indexed_documents": db.query(IngestedDocument).count()}

@app.get("/v1/dashboard/documents", tags=["Dashboard"])
async def list_dashboard_documents(db: Session = Depends(get_db),
                                   admin: User = Depends(dashboard_admin)):
    rows = db.query(IngestedDocument).order_by(IngestedDocument.ingested_at.desc()).all()

    def _groesse(pfad: str | None) -> int | None:
        #--- Aus dem Dateisystem statt aus einer Spalte: eine Migration waere hier
        #--- nur Beiwerk. Ist die Datei inzwischen weg, ist None die ehrliche Antwort.
        if not pfad:
            return None
        try:
            return os.path.getsize(pfad)
        except OSError:
            return None

    return {"documents": [
        {"id": r.id, "filename": r.filename, "chunk_count": r.chunk_count,
         "size_bytes": _groesse(r.file_path),
         "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None} for r in rows
    ]}

@app.delete("/v1/dashboard/documents", tags=["Dashboard"])
async def clear_dashboard_documents(db: Session = Depends(get_db),
                                    admin: User = Depends(dashboard_admin),
                                    csrf = Depends(check_dashboard_csrf)):
    '''Leert die ganze Collection: Qdrant-Chunks und Datensaetze.

    Die Dateien im Arbeitsverzeichnis bleiben liegen -- geloescht wird der INDEX,
    nicht das Original. Wer die Datei loswill, nimmt den Upload-Endpunkt.'''
    rows = db.query(IngestedDocument).all()
    from rag_backend.ingest import _delete_qdrant_chunks
    weg, fehler = 0, 0
    for r in rows:
        try:
            _delete_qdrant_chunks(r.filename)
        except Exception as e:
            fehler += 1
            log.warning(f"Qdrant-Loeschung fuer {r.filename} fehlgeschlagen: {e}")
        db.delete(r)
        weg += 1
    db.commit()
    return {"status": "ok", "deleted": weg, "errors": fehler}


@app.delete("/v1/dashboard/documents/{doc_id}", tags=["Dashboard"])
async def delete_dashboard_document(doc_id: int, db: Session = Depends(get_db),
                                    admin: User = Depends(dashboard_admin),
                                    csrf = Depends(check_dashboard_csrf)):
    doc = db.get(IngestedDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden.")
    filename = doc.filename
    try:
        from rag_backend.ingest import _delete_qdrant_chunks
        _delete_qdrant_chunks(filename)
    except Exception as e:
        log.warning(f"Fehler bei Qdrant-Loeschung fuer {filename}: {e}")
    db.delete(doc)
    db.commit()
    return {"status": "ok", "message": f"Dokument {filename} erfolgreich gelöscht."}

_EVAL_LOGS_DIR = os.getenv("EVAL_LOGS_DIR", "/app/evals/Run_Logs")


def _summarize_eval_file(fpath, fname):
    # Liest eine Run-Log-Datei und extrahiert die Zusammenfassung.
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    suite = d.get("suite")
    if not suite:
        low = fname.lower()
        if low.startswith("gaia"):
            suite = "GAIA-Subset"
        elif low.startswith("humaneval"):
            suite = "HumanEval"
        else:
            suite = fname.rsplit(".", 1)[0]
    metric, value = d.get("metric"), d.get("value")
    if value is None:
        if d.get("accuracy") is not None:
            metric, value = metric or "Accuracy", d.get("accuracy")
        elif d.get("score") is not None:
            metric, value = metric or "pass@1", d.get("score")
    ts = d.get("timestamp") or d.get("ts")
    if not ts:
        try:
            ts = datetime.fromtimestamp(os.stat(fpath).st_mtime, tz=timezone.utc).isoformat()
        except Exception:
            ts = None
    return {"ts": ts, "suite": suite, "config": d.get("config") or d.get("model"),
            "task": d.get("task"), "metric": metric, "value": value,
            "notes": d.get("notes"), "file": fname}


@app.get("/v1/dashboard/evals", tags=["Dashboard"])
async def dashboard_evals(admin: User = Depends(dashboard_admin)):
    # Quelle der Wahrheit sind die Run-Log-Dateien in Run_Logs/, nicht die DB.
    base = os.path.realpath(_EVAL_LOGS_DIR)
    rows = []
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if os.path.isfile(fpath):
                    rows.append(_summarize_eval_file(fpath, fname))
        rows.sort(key=lambda r: (r["ts"] or ""), reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Eval-Logs nicht lesen: {e}")
    return {"evals": rows}


@app.delete("/v1/dashboard/evals", tags=["Dashboard"])
async def dashboard_clear_evals(admin: User = Depends(dashboard_admin),
                                csrf = Depends(check_dashboard_csrf)):
    # Loescht die Run-Log-DATEIEN (bewusste, im Dashboard bestaetigte Aktion).
    base = os.path.realpath(_EVAL_LOGS_DIR)
    deleted = 0
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1
        return {"status": "ok", "message": f"{deleted} Eval-Log-Datei(en) gelöscht."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Raw-Viewer fuer eine einzelne Run-Log-Datei. Das Backend liest die gemounteten
# Dateien direkt.

@app.get("/v1/dashboard/eval_logs", tags=["Dashboard"])
async def dashboard_eval_logs(admin: User = Depends(dashboard_admin)):
    base = os.path.realpath(_EVAL_LOGS_DIR)
    files = []
    try:
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                if not os.path.isfile(fpath):
                    continue
                st = os.stat(fpath)
                files.append({
                    "name": fname,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                })
        files.sort(key=lambda r: r["mtime"], reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Run-Logs nicht lesen: {e}")
    return {"dir": base, "files": files}

@app.get("/v1/dashboard/eval_logs/{name}", tags=["Dashboard"])
async def dashboard_eval_log(name: str, admin: User = Depends(dashboard_admin)):
    # Path-Traversal-Schutz: nur Basename, nur .json, und die aufgeloeste Datei
    # MUSS direkt im Run_Logs-Verzeichnis liegen.
    if name != os.path.basename(name) or not name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname.")
    base = os.path.realpath(_EVAL_LOGS_DIR)
    target = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(target) != base or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Log-Datei nicht gefunden.")
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Konnte Log nicht lesen: {e}")

# ---- Voice-Konsole: Dashboard-Chat mit Orb-Events #---

class DashboardChatIn(BaseModel):
    #--- Obergrenzen gegen Speicher-Erschoepfung: der Body wird vor jeder
    #--- weiteren Pruefung komplett eingelesen.
    message: str = Field(max_length=int(os.getenv("DASHBOARD_MAX_MESSAGE_CHARS", "200000")))
    #--- KEIN history-Feld: der Verlauf kommt ausschliesslich aus der DB
    #--- (_chat_history_from_db an der session_id).
    session_id: int | None = None
    #--- data:-URIs aus Dateidialog oder Zwischenablage. Gefiltert nach denselben
    #--- Regeln wie im OpenAI-Proxy.
    images: list[str] = Field(default=[], max_length=8)


def _dashboard_images(raw) -> list:
    '''Wie extract_image_urls, nur fuer die flache Liste des Argus-Chats.

    Dieselben zwei Grenzen (Anzahl und Groesse je URI) und dieselbe Beschraenkung auf
    data:image -- eine http-URL wuerde das Modell-Backend dazu bringen, im Namen des
    Nutzers eine fremde Adresse abzurufen (SSRF ueber den Umweg Bild).'''
    out = []
    for u in (raw or []):
        if not isinstance(u, str) or not u.startswith("data:image/"):
            continue
        if len(u) > _MAX_IMAGE_DATA_CHARS:
            continue
        out.append(u)
        if len(out) >= _MAX_IMAGES:
            break
    return out


def _orb_sse(d: dict) -> str:
    return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


def _chat_title(db, session_id: int) -> str:
    # Titel = erste Nutzernachricht, gekuerzt. Bewusst ohne eigene title-Spalte:
    # eine Migration hier wuerde die Alembic-Kette ohne Setup4 zerreissen.
    m = (db.query(Message).filter_by(session_id=session_id, sender="user")
         .order_by(Message.id.asc()).first())
    if not m:
        return "Neuer Chat"
    try:
        first_line = decrypt_msg(m.content_encrypted).strip().splitlines()[0]
    except Exception as e:
        # Mit lautem Log, nicht still: ein zu breites except hat hier schon einen
        # NameError als Krypto-Problem getarnt.
        log.warning(f"Chat-Titel fuer Session {session_id} nicht lesbar: {type(e).__name__}: {e}")
        return "Neuer Chat"
    t = re.sub(r"\s+", " ", first_line).strip()
    if not t:
        return "Neuer Chat"
    return t[:60] + "…" if len(t) > 60 else t


def _chat_history_from_db(db, session_id: int, limit: int = 10) -> list:
    # Verlauf kommt aus der DB, nicht vom Client: ein manipulierter Client
    # koennte dem Agenten sonst eine Vorgeschichte unterschieben.
    rows = (db.query(Message).filter_by(session_id=session_id)
            .order_by(Message.id.desc()).limit(limit).all())
    out = []
    for m in reversed(rows):
        try:
            content = decrypt_msg(m.content_encrypted)
        except Exception as e:
            log.warning(f"Verlauf: Nachricht {m.id} uebersprungen ({type(e).__name__}: {e})")
            continue
        if not content:
            continue
        if m.sender == "user":
            out.append(HumanMessage(content=content))
        else:
            out.append(AIMessage(content=strip_status_lines(content)))
    return out


@app.get("/v1/dashboard/chats", tags=["Dashboard"])
def dashboard_chats(db: Session = Depends(get_db), user: User = Depends(dashboard_user)):
    #--- Verlaeufe sind an das Konto gebunden. Der Agent haengt sein Gedaechtnis
    #--- an dieselbe ID -- jedes Konto hat damit sein eigenes.
    rows = (db.query(ChatSession).filter_by(user_id=user.id)
            .order_by(ChatSession.created_at.desc()).limit(200).all())
    out = []
    for s in rows:
        last = (db.query(Message).filter_by(session_id=s.id)
                .order_by(Message.id.desc()).first())
        out.append({
            "id": s.id,
            "title": _chat_title(db, s.id),
            "messages": db.query(Message).filter_by(session_id=s.id).count(),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_at": last.timestamp.isoformat() if last and last.timestamp else None,
        })
    return {"chats": out}


@app.get("/v1/dashboard/chats/{chat_id}", tags=["Dashboard"])
def dashboard_chat_load(chat_id: int, db: Session = Depends(get_db),
                        user: User = Depends(dashboard_user)):
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    msgs = db.query(Message).filter_by(session_id=chat_id).order_by(Message.id.asc()).all()
    out = []
    for m in msgs:
        try:
            content = decrypt_msg(m.content_encrypted)
        except Exception as e:
            # Fernet-Key gewechselt: die einzelne Zeile bleibt sichtbar, statt den
            # ganzen Verlauf unlesbar zu machen. Mit Log.
            log.warning(f"Nachricht {m.id} nicht entschluesselbar: {type(e).__name__}: {e}")
            content = "[nicht entschluesselbar]"
        #--- Werkzeugschritte sind Beiwerk: ist die Spalte leer oder der Schluessel
        #--- gewechselt, fehlt eben der Block. Die Nachricht selbst bleibt lesbar.
        steps = None
        if getattr(m, "steps_encrypted", None):
            try:
                steps = json.loads(decrypt_msg(m.steps_encrypted))
            except Exception as e:
                log.warning(f"Schritte zu Nachricht {m.id} nicht lesbar: {type(e).__name__}")
        out.append({
            #--- id mitliefern: die Oberflaeche haengt Aktionen (bearbeiten, erneut
            #--- versuchen, loeschen) an die einzelne Nachricht und braucht dafuer
            #--- einen Bezug, der einen Neuaufbau des Verlaufs uebersteht.
            "id": m.id,
            "role": "user" if m.sender == "user" else "assistant",
            "content": content,
            "steps": steps,
            #--- Dauer der Antwort: die Chat-Ansicht zeigt sie im Nachrichtenkopf
            #--- neben der Uhrzeit. Steht nur an Assistenz-Nachrichten.
            "latency_ms": m.latency_ms,
            "ts": m.timestamp.isoformat() if m.timestamp else None,
        })
    return {"id": s.id, "title": _chat_title(db, s.id), "messages": out}


@app.delete("/v1/dashboard/chats/{chat_id}/messages/{message_id}", tags=["Dashboard"])
def dashboard_message_delete(chat_id: int, message_id: int, following: bool = False,
                             db: Session = Depends(get_db),
                             user: User = Depends(dashboard_user),
                             csrf = Depends(check_dashboard_csrf)):
    '''Einzelne Nachricht loeschen, optional mitsamt allem, was danach kam.

    following=True ist der Fall "nochmal versuchen" und "Frage bearbeiten": eine
    Antwort verwerfen heisst auch, alles zu verwerfen, was auf ihr aufbaut -- sonst
    stuenden im Verlauf Folgeantworten zu einer Frage, die es nicht mehr gibt.
    Die Session-ID muss passen: sonst liesse sich ueber eine fremde chat_id jede
    beliebige Nachricht loeschen.
    '''
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    msg = db.query(Message).filter_by(id=message_id, session_id=chat_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Nachricht nicht gefunden")
    if following:
        # Reihenfolge ueber die ID, nicht ueber den Zeitstempel.
        q = db.query(Message).filter(Message.session_id == chat_id,
                                     Message.id >= message_id)
    else:
        q = db.query(Message).filter(Message.id == message_id)
    count = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return {"status": "ok", "deleted": count}


@app.post("/v1/dashboard/chats", tags=["Dashboard"])
def dashboard_chat_new(db: Session = Depends(get_db), user: User = Depends(dashboard_user),
                       csrf = Depends(check_dashboard_csrf)):
    s = ChatSession(user_id=user.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "title": "Neuer Chat", "messages": []}


@app.delete("/v1/dashboard/chats/{chat_id}", tags=["Dashboard"])
def dashboard_chat_delete(chat_id: int, db: Session = Depends(get_db),
                          user: User = Depends(dashboard_user),
                          csrf = Depends(check_dashboard_csrf)):
    s = db.query(ChatSession).filter_by(id=chat_id, user_id=user.id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Chat nicht gefunden")
    # Nachrichten zuerst: auf messages.session_id liegt ein Fremdschluessel.
    db.query(Message).filter_by(session_id=chat_id).delete()
    db.delete(s)
    db.commit()
    return {"status": "deleted", "id": chat_id}


def _genannte_uploads(text: str, user_id: int | None = None, role: str | None = None) -> list:
    '''Namen von Dateien aus dem eigenen Bereich Dateien (fuer alle Konten) sowie
    fuer Administratoren zusaetzlich aus dem gemeinsamen Uploads-Ordner, die in der
    Nachricht vorkommen. Die Oberflaeche setzt beim Hineinziehen den Dateinamen in die
    Nachricht; wer ihn von Hand tippt, wird genauso erfasst.'''
    if not text:
        return []
    klein = text.lower()
    treffer = []

    # 1. Eigener Ordner 'Dateien' des Kontos (fuer alle Rollen)
    if user_id and isinstance(user_id, int) and user_id > 0:
        try:
            from rag_backend import user_files
            u_dateien = user_files.get_user_dir(user_id, "dateien", create=False)
            if u_dateien.is_dir():
                for i, p in enumerate(u_dateien.iterdir()):
                    if i >= 5000 or len(treffer) >= 50:
                        break
                    if len(p.name) >= 5 and p.name.lower() in klein:
                        if p.name not in treffer:
                            treffer.append(p.name)
        except OSError:
            pass

    # 2. Fuer Administratoren (oder wenn role None / admin): gemeinsamer Uploads-Ordner
    if role is None or role == "admin":
        if _UPLOAD_DIR.is_dir():
            try:
                for i, p in enumerate(_UPLOAD_DIR.iterdir()):
                    if i >= 5000 or len(treffer) >= 50:
                        break
                    if len(p.name) >= 5 and p.name.lower() in klein:
                        if p.name not in treffer:
                            treffer.append(p.name)
            except OSError:
                pass

    return treffer


class DashboardWebSearchIn(BaseModel):
    query: str
    sources: int = 3


@app.post("/v1/dashboard/web_search", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_WEB_SEARCH_RATE_LIMIT", "20/minute"))
async def dashboard_web_search(body: DashboardWebSearchIn, request: Request,
                               user: User = Depends(dashboard_user),
                               csrf = Depends(check_dashboard_csrf)):
    #--- Websuche fuer Argus Desktop (Argus Code): dieselbe Engine wie das Werkzeug
    #--- web_search (SearxNG -> Ranking -> Volltext, SSRF-Schutz, Injection-Check),
    #--- aber ohne Agenten-Turn. SearxNG ist bewusst nicht auf dem Host veroeffentlicht.
    from rag_backend.agent import _search_and_read, quellen_liste, quellen_zuruecksetzen
    q = (body.query or "").strip()[:300]
    if not q:
        raise HTTPException(status_code=400, detail="Leere Suchanfrage.")
    quellen_zuruecksetzen()
    text = await _search_and_read(
        q,
        n_sources=max(1, min(int(body.sources or 3), 5)),
        per_source_chars=int(os.getenv("WEB_SEARCH_PER_SOURCE_CHARS", 7000)),
        injection_guard=True,
    )
    return {"query": q, "text": text, "sources": quellen_liste()}


@app.post("/v1/dashboard/chat", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_CHAT_RATE_LIMIT", "60/minute"))
async def dashboard_chat(body: DashboardChatIn, request: Request,
                         user: User = Depends(dashboard_user),
                         csrf = Depends(check_dashboard_csrf)):
    # Streamt getypte SSE-Events, die die Orb-Zustaende treiben: state / reasoning
    # / tool / token / sources / done.
    uid = user.id

    #--- Reihenfolge zaehlt: erst den bisherigen Verlauf lesen, DANN die neue
    #--- Nachricht schreiben -- sonst stuende die Frage doppelt im Kontext.
    with SessionLocal() as db0:
        chat_session = None
        if body.session_id:
            chat_session = db0.query(ChatSession).filter_by(
                id=body.session_id, user_id=uid).first()
        if not chat_session:
            chat_session = ChatSession(user_id=uid)
            db0.add(chat_session)
            db0.commit()
            db0.refresh(chat_session)
        sid = chat_session.id
        chat_history = _chat_history_from_db(db0, sid)
        #--- Bildmarke wie im Proxy, sonst steht im Verlauf eine leere Nutzerzeile.
        images = _dashboard_images(body.images)
        db0.add(Message(session_id=sid, sender="user",
                        content_encrypted=encrypt_msg(
                            ("[Bild angehaengt] " if images else "") + body.message)))
        db0.commit()

    agent_payload = {
        "input": body.message,
        "chat_history": chat_history,
        "user_id": uid,
        #--- Konten der Rolle 'user' bekommen keine Cloud-, Datei- und
        #--- Sandbox-Werkzeuge (agent._ADMIN_ONLY_TOOLS). Nie None: None gilt im
        #--- Agenten als interner Lauf mit allen Werkzeugen.
        "user_role": getattr(user, "role", None) or "user",
        "images": images,
        #--- Beigelegte Dateien: das Gespraech gilt damit sofort als Gespraech mit
        #--- Firmendaten (agent.beigelegte_dateien_vermerken). Gilt fuer alle Konten.
        "attached_files": _genannte_uploads(body.message, user_id=uid, role=getattr(user, "role", None)),
        #--- Herkunft fuer die Missions-Zustellung: das Ergebnis kommt in diese Session.
        "channel": "dashboard",
        "dashboard_session_id": sid,
    }

    async def gen():
        #--- Laufzeitmessung wie im Proxy. Ohne sie zaehlte der Dashboard-Chat in
        #--- keine Request-Kennzahl ein.
        _t0 = time.monotonic()
        _ttft_ms = None
        _recorded = False
        stream = agent_executor.astream(agent_payload)
        first_answer = True
        answer_parts = []
        #--- Mitschrift des Turns. Sie geht am Ende verschluesselt in die Datenbank,
        #--- damit ein neu geladener Verlauf nicht nur den nackten Antworttext zeigt.
        schritte = []
        quellen = []
        dokumente = []
        dateien = []
        #--- Lauf-ID eines Werkzeugs -> Index seines Schritts, fuer die Zuordnung
        #--- einer Abweisung durch die Sperre.
        lauf_zu_schritt = {}
        #--- Laufender Werkzeugschritt: (Startzeit, Index in schritte). Seine Dauer
        #--- steht erst fest, wenn etwas anderes kommt -- dann wird er geschlossen.
        offener_schritt = None
        #--- Text nach einem Werkzeugschritt beginnt einen neuen Absatz. Sonst klebt
        #--- eine Ankuendigung ("Ich pruefe ...") direkt an der Antwort danach.
        nach_werkzeug = False

        def _record_once():
            #--- Genau einmal je Turn. Zaehlt die ROH gestreamte Ausgabe, nicht die
            #--- bereinigte -- der Proxy zaehlt ebenso.
            nonlocal _recorded
            if _recorded:
                return
            _recorded = True
            _raw = "".join(answer_parts)
            _total = round((time.monotonic() - _t0) * 1000.0)
            try:
                metrics.record_request(
                    ttft_ms=round(_ttft_ms) if _ttft_ms is not None else None,
                    total_ms=_total,
                    completion_chars=len(_raw),
                )
            except Exception:
                pass

        try:
            # Eigener Event-Typ, KEIN state-Event: state-Werte gehen direkt in
            # setOrbState(), ein unbekannter Wert wuerde den Orb zuruecksetzen.
            yield _orb_sse({"type": "session", "session_id": sid})
            yield _orb_sse({"type": "state", "value": "thinking"})
            async for chunk in stream:
                if await request.is_disconnected():
                    break
                if not isinstance(chunk, dict):
                    continue
                #--- Freigabe-Anfrage: die Chat-Ansicht baut daraus eine Karte mit
                #    Knoepfen. Muss VOR den uebrigen Zweigen stehen -- ein
                #    confirmation-Chunk hat weder status noch output.
                conf = chunk.get("confirmation")
                if conf:
                    yield _orb_sse({"type": "confirmation", **conf})
                    continue
                #--- Die Sperre nach Firmendaten hat ein Werkzeug abgewiesen. Der
                #--- Schritt wird als gesperrt gekennzeichnet -- sonst saehe eine
                #--- abgewiesene Websuche aus wie eine ausgefuehrte.
                gesperrt = chunk.get("tool_blocked")
                if gesperrt:
                    _gi = lauf_zu_schritt.get(gesperrt.get("run_id"))
                    if _gi is None and offener_schritt is not None:
                        _gi = offener_schritt[1]
                    if _gi is not None and 0 <= _gi < len(schritte):
                        schritte[_gi]["gesperrt"] = True
                        yield _orb_sse({"type": "tool_blocked", "index": _gi})
                    continue
                #--- Jeder andere Chunk beendet den laufenden Werkzeugschritt.
                if offener_schritt is not None:
                    _t, _i = offener_schritt
                    _ms = round((time.monotonic() - _t) * 1000)
                    schritte[_i]["dauer_ms"] = _ms
                    offener_schritt = None
                    yield _orb_sse({"type": "tool_done", "index": _i, "dauer_ms": _ms})

                reasoning_chunk = chunk.get("reasoning")
                if reasoning_chunk:
                    schritte.append({"art": "denken", "text": reasoning_chunk})
                    yield _orb_sse({"type": "reasoning", "text": reasoning_chunk})
                    continue
                status = chunk.get("status")
                if status:
                    # TTFT = erste SICHTBARE Ausgabe. Eine Statuszeile zaehlt mit,
                    # ein Reasoning-Chunk nicht -- gleiche Abgrenzung wie im Proxy.
                    if _ttft_ms is None:
                        _ttft_ms = (time.monotonic() - _t0) * 1000.0
                    # Erstes Zeichen der Statuszeile ist das Anzeige-Emoji.
                    schritte.append({"art": "werkzeug", "emoji": status[0], "text": status.strip()})
                    offener_schritt = (time.monotonic(), len(schritte) - 1)
                    if chunk.get("run_id"):
                        lauf_zu_schritt[chunk["run_id"]] = len(schritte) - 1
                    yield _orb_sse({"type": "tool", "emoji": status[0], "label": status.strip(),
                                    "index": len(schritte) - 1})
                    nach_werkzeug = True
                    continue
                #--- Quellen des Turns, durchlaufend nummeriert im Agenten. Sie tragen
                #--- die Nummern, mit denen die Antwort sie belegt.
                srcs = chunk.get("sources")
                if srcs:
                    quellen.extend(srcs)
                    yield _orb_sse({"type": "sources", "items": srcs})
                    continue
                #--- Dokumente der Dokumentensuche: jedes Mal die ganze Liste des
                #--- Turns (je Datei die beste Relevanz), die Oberflaeche ersetzt
                #--- ihre Zeile.
                dsrcs = chunk.get("doc_sources")
                if dsrcs:
                    dokumente[:] = dsrcs
                    yield _orb_sse({"type": "doc_sources", "items": dsrcs})
                    continue
                #--- Neu erzeugte Dateien (Ergebnisse): als Pille zum Download anbieten
                fitems = chunk.get("files")
                if fitems:
                    dateien.extend(fitems)
                    yield _orb_sse({"type": "files", "items": fitems})
                    continue
                out = chunk.get("output")
                if not out:
                    continue
                if nach_werkzeug:
                    nach_werkzeug = False
                    bisher = "".join(answer_parts)
                    if bisher.strip() and not bisher.endswith(("\n", " ")) and not out.startswith(("\n", " ")):
                        out = "\n\n" + out
                if _ttft_ms is None:
                    _ttft_ms = (time.monotonic() - _t0) * 1000.0
                if first_answer:
                    first_answer = False
                    yield _orb_sse({"type": "state", "value": "speaking"})
                answer_parts.append(out)
                yield _orb_sse({"type": "token", "text": out})

            #--- Letzter Schritt: er wird von keinem weiteren Chunk mehr geschlossen.
            if offener_schritt is not None:
                _t, _i = offener_schritt
                _ms = round((time.monotonic() - _t) * 1000)
                schritte[_i]["dauer_ms"] = _ms
                offener_schritt = None
                yield _orb_sse({"type": "tool_done", "index": _i, "dauer_ms": _ms})

            #--- Kennzahlen JETZT erfassen: der Missions-Follow wartet u.U. Minuten
            #--- auf einen Cloud-Worker, das ist keine Antwortlatenz.
            _record_once()

            #--- Mission-Follow wie im Proxy: hat dieser Turn eine Mission gestartet,
            #--- bleibt der Stream offen. Der Argus-Chat hat keine Push-API.
            if os.getenv("CLOUD_MISSION_FOLLOW", "true").lower() == "true":
                try:
                    from rag_backend.cloud_agents import missions as _missions
                    _follow_ids = await asyncio.to_thread(_missions.active_for_dashboard, sid)
                except Exception as e:
                    log.warning(f"Argus-Chat Mission-Follow: Start fehlgeschlagen: {e}")
                    _follow_ids = []
                if _follow_ids:
                    # Registrierung VOR dem ersten Poll: ab jetzt ueberspringt
                    # _deliver die Zustellung fuer diese IDs.
                    _missions._FOLLOWED.update(_follow_ids)
                    _streamed = set()
                    try:
                        _deadline = time.monotonic() + int(os.getenv("CLOUD_MISSION_FOLLOW_MAX_SECONDS", "240"))
                        _poll = max(2, int(os.getenv("CLOUD_MISSION_FOLLOW_POLL_SECONDS", "4")))
                        _pending = list(_follow_ids)
                        _last_step: dict = {}
                        log.info(f"Argus-Chat Mission-Follow: begleite {_pending} in Session {sid}.")
                        while _pending and time.monotonic() < _deadline:
                            if await request.is_disconnected():
                                log.info("Argus-Chat Mission-Follow: Client weg -- Zustellung uebernimmt.")
                                break
                            # verhindert Reflection-Start mitten im Follow
                            request.app.state.last_activity = datetime.now(timezone.utc)
                            for _mid in list(_pending):
                                _status, _step = await asyncio.to_thread(_missions.mission_step, _mid)
                                if _status == "missing":
                                    _pending.remove(_mid)
                                    continue
                                if _status in _missions._TERMINAL_NEEDLES:
                                    _text = await asyncio.to_thread(_missions.delivery_text, _mid)
                                    if _text:
                                        # Ergebnis streamen UND in answer_parts,
                                        # damit es mitpersistiert wird.
                                        yield _orb_sse({"type": "token", "text": "\n\n" + _text})
                                        answer_parts.append("\n\n" + _text)
                                    _streamed.add(_mid)
                                    _pending.remove(_mid)
                                    _missions._FOLLOWED.discard(_mid)
                                    continue
                                if _step and _last_step.get(_mid) != _step:
                                    _last_step[_mid] = _step
                                    # Fortschritt als Werkzeug-Schritt: sichtbar als
                                    # Zeile, aber nicht Teil der Antwort.
                                    yield _orb_sse({"type": "tool", "emoji": "⏳",
                                                    "label": f"⏳ Mission {_mid}: {_step}"})
                            if _pending:
                                await asyncio.sleep(_poll)
                        if _pending and not await request.is_disconnected():
                            _ids = ", ".join(str(i) for i in _pending)
                            _hint = (f"\n\nMission {_ids} läuft weiter im Hintergrund -- "
                                     "das Ergebnis erscheint automatisch hier im Chat.")
                            yield _orb_sse({"type": "token", "text": _hint})
                            answer_parts.append(_hint)
                    except Exception as e:
                        # Follow ist Komfort -- ein Fehler darf den Turn nicht abreissen.
                        log.warning(f"Argus-Chat Mission-Follow abgebrochen: {e}")
                    finally:
                        _missions._FOLLOWED.difference_update(_follow_ids)
                        #--- Schliesst das Rennen zwischen Abmeldung und Missions-
                        #--- Ende: sonst uebersprang _deliver die Zustellung, waehrend
                        #--- hier schon niemand mehr zuhoert.
                        for _mid in _follow_ids:
                            if _mid in _streamed:
                                continue
                            try:
                                _st, _ = await asyncio.to_thread(_missions.mission_step, _mid)
                                if _st in _missions._TERMINAL_NEEDLES:
                                    _txt = await asyncio.to_thread(_missions.delivery_text, _mid)
                                    if _txt:
                                        await asyncio.to_thread(
                                            _missions._append_dashboard_message, sid, _txt)
                            except Exception as e:
                                log.warning(f"Nachreichen von Mission {_mid} fehlgeschlagen: {e}")

            yield _orb_sse({"type": "done", "session_id": sid})
        finally:
            await stream.aclose()
            # Abgebrochener Turn (Tab zu, Stop-Knopf): er hat GPU-Zeit gekostet und
            # gehoert in den Schnitt.
            _record_once()
            raw_output = "".join(answer_parts)
            total_ms = round((time.monotonic() - _t0) * 1000.0)
            ttft_val = round(_ttft_ms) if _ttft_ms is not None else None
            tokens_sec_val = None
            if total_ms and raw_output:
                secs = total_ms / 1000.0
                if secs > 0:
                    tokens_sec_val = round((len(raw_output) / metrics._CHARS_PER_TOKEN) / secs, 1)
            # Auch bei Abbruch persistieren: die Teilantwort gehoert zum Verlauf.
            # Eigene DB-Session -- die des Requests ist hier zu.
            answer = strip_status_lines(raw_output).strip()
            if answer:
                try:
                    with SessionLocal() as db2:
                        #--- Denk-Chunks zu EINEM Block zusammenziehen: sie kommen
                        #--- tokenweise, einzeln gespeichert waeren es Hunderte Eintraege.
                        gefaltet, denk = [], []
                        for sch in schritte:
                            if sch["art"] == "denken":
                                denk.append(sch["text"])
                                continue
                            if denk:
                                gefaltet.append({"art": "denken", "text": "".join(denk)})
                                denk = []
                            gefaltet.append(sch)
                        if denk:
                            gefaltet.append({"art": "denken", "text": "".join(denk)})
                        mitschrift = encrypt_msg(json.dumps(
                            {"schritte": gefaltet, "quellen": quellen,
                             "dokumente": dokumente, "dateien": dateien},
                            ensure_ascii=False)) if (gefaltet or quellen or dokumente or dateien) else None
                        db2.add(Message(session_id=sid, sender="assistant",
                                        content_encrypted=encrypt_msg(answer),
                                        steps_encrypted=mitschrift,
                                        latency_ms=total_ms, ttft_ms=ttft_val,
                                        tokens_per_sec=tokens_sec_val))
                        db2.commit()
                except Exception as e:
                    log.warning(f"Dashboard-Chat: Antwort nicht persistierbar: {e}")

    return StreamingResponse(gen(), media_type="text/event-stream")


def _voice_auth_headers() -> dict[str, str]:
    # STT und TTS pruefen denselben API-Key wie das LLM (llm_api_key).
    from rag_backend.crypto_utils import load_llm_api_key
    key = (load_llm_api_key() or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


@app.post("/v1/dashboard/stt", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_VOICE_RATE_LIMIT", "60/minute"))
async def dashboard_stt(request: Request, file: UploadFile = File(...),
                        user: User = Depends(dashboard_user),
                        csrf = Depends(check_dashboard_csrf)):
    # Browser nimmt Audio auf (MediaRecorder) -> rag-backend -> stt-service.
    import httpx
    url = os.getenv("STT_SERVICE_URL", "http://stt-service:8003").rstrip("/") + "/v1/audio/transcriptions"
    #--- Stueckweise lesen und frueh abbrechen: Starlette begrenzt Datei-Parts
    #--- nicht, ein riesiger Upload laege sonst komplett im Speicher.
    max_bytes = int(os.getenv("STT_MAX_UPLOAD_MB", "50")) * 1024 * 1024
    chunks, total = [], 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Aufnahme zu gross.")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        headers = _voice_auth_headers()
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                url,
                headers=headers,
                files={"file": (file.filename or "audio.webm", data, file.content_type or "audio/webm")},
            )
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("STT-Dienst-Aufruf fehlgeschlagen")
        raise HTTPException(status_code=502, detail="STT-Dienst nicht erreichbar.")


class DashboardTtsIn(BaseModel):
    input: str
    voice: str | None = None


@app.post("/v1/dashboard/tts", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_VOICE_RATE_LIMIT", "60/minute"))
async def dashboard_tts(body: DashboardTtsIn, request: Request,
                        user: User = Depends(dashboard_user),
                        csrf = Depends(check_dashboard_csrf)):
    # Antwort-Text -> tts-service (intern) -> Audio-Bytes, vom Frontend abgespielt.
    import httpx
    url = os.getenv("TTS_SERVICE_URL", "http://tts-service:8002").rstrip("/") + "/v1/audio/speech"
    payload = {"model": os.getenv("AUDIO_TTS_MODEL", "tts-1"), "input": body.input,
               "voice": body.voice or os.getenv("AUDIO_TTS_VOICE", "alloy")}
    try:
        headers = _voice_auth_headers()
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return Response(content=r.content, media_type=r.headers.get("content-type", "audio/wav"))
    except Exception as e:
        log.exception("TTS-Dienst-Aufruf fehlgeschlagen")
        raise HTTPException(status_code=502, detail="TTS-Dienst nicht erreichbar.")


# ---- Dateien: hochladen, indexieren #---
#--- Ziel ist der Uploads-Ordner des Arbeitsverzeichnisses. Genau dort darf der
#--- Agent lesen -- eine Datei anderswo im Workspace umginge die Pfad-Whitelist.

_WORKSPACE_ROOT = Path(
    os.getenv("ARGUS_WORKSPACE")
    or os.getenv("WORKSPACE_CONTAINER_DIR")
    or "/host/argus_workspace"
)
_UPLOAD_DIR = _WORKSPACE_ROOT / "Uploads"
_MAX_UPLOAD_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(500 * 1024 * 1024)))

#--- Aufraeumen. In den Chat gezogene Dateien sind Arbeitsmaterial fuer EINE
#--- Unterhaltung; was dauerhaft auffindbar bleiben soll, gehoert ueber
#--- ingest_document in den Index, nicht in diesen Ordner. Ohne Frist saehe man
#--- hier nach einem halben Jahr jeden je geprueften Vertrag liegen.
#--- Sofort nach dem Lesen loeschen geht NICHT: die erste Rueckfrage in derselben
#--- Unterhaltung liesse read_document sonst ins Leere laufen.
#--- 0 Stunden = Aufraeumen aus.
_UPLOAD_RETENTION_HOURS = float(os.getenv("UPLOAD_RETENTION_HOURS", "24"))
_UPLOAD_SWEEP_SECONDS = int(os.getenv("UPLOAD_SWEEP_SECONDS", "3600"))


def _sweep_uploads_once() -> int:
    '''Entfernt Dateien im Uploads-Ordner, die aelter als die Frist sind,
    sowie veraltete Benutzerdateien (users/*/Dateien und users/*/Ergebnisse).
    Liefert die Gesamtzahl entfernter Dateien.'''
    weg = 0
    if _UPLOAD_RETENTION_HOURS > 0 and _UPLOAD_DIR.is_dir():
        try:
            grenze = time.time() - _UPLOAD_RETENTION_HOURS * 3600
            for p in _UPLOAD_DIR.iterdir():
                try:
                    if not p.is_file() or p.stat().st_mtime >= grenze:
                        continue
                    p.unlink()
                    weg += 1
                    log.info(f"Upload aufgeraeumt (aelter als {_UPLOAD_RETENTION_HOURS}h): {p.name}")
                except Exception as e:
                    log.warning(f"Upload {p.name} liess sich nicht entfernen: {e}")
        except Exception:
            pass

    try:
        from rag_backend import user_files
        weg += user_files.sweep_user_files_once()
    except Exception as e:
        log.warning(f"Benutzerdateien-Aufraeumen fehlgeschlagen: {e}")

    return weg


async def _uploads_sweeper():
    '''Einmal beim Start, danach im Takt. Ein Fehler beendet die Schleife nicht --
    Aufraeumen ist Pflege, kein Betriebszustand.'''
    while True:
        try:
            n = await asyncio.to_thread(_sweep_uploads_once)
            if n:
                log.info(f"Uploads aufgeraeumt: {n} Datei(en) entfernt.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"Uploads-Aufraeumen fehlgeschlagen: {e}")
        await asyncio.sleep(_UPLOAD_SWEEP_SECONDS)
#--- Endungs-Allowlist statt Blocklist, deckungsgleich mit dem, was load_file
#--- lesen kann, plus Bilder. .env/.log fehlen bewusst -- solche Dateien tragen
#--- typischerweise Secrets.
#--- Archive sind bewusst nicht dabei: Pfad-Ausbruch, Kompressionsbomben und
#--- Dateizahl muessten alle einzeln abgefangen werden.
_UPLOAD_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".epub", ".txt", ".md",
    ".html", ".htm", ".json", ".yaml", ".yml", ".csv", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}
#--- Dateitypen, aus denen die Indexierung Text ziehen kann.
_INGESTABLE_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".epub", ".txt", ".md",
    ".html", ".htm", ".json", ".yaml", ".yml", ".csv",
}


def _safe_upload_name(raw: str) -> str:
    '''Baut aus einem beliebigen Client-Dateinamen einen harmlosen Basename.

    Ein Upload-Name kommt vom Browser und ist damit Fremdeingabe: '../../run/secrets/
    ssh_key' oder ein Name mit Doppelpunkt (alternativer NTFS-Datenstrom) muss hier
    scheitern, nicht erst am Dateisystem.'''
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", name).lstrip(".")
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Unbrauchbarer Dateiname.")
    return name[:180]


def _upload_path(name: str) -> Path:
    '''Loest einen Namen im Uploads-Ordner auf und stellt sicher, dass er dort bleibt.'''
    target = (_UPLOAD_DIR / _safe_upload_name(name)).resolve(strict=False)
    try:
        target.relative_to(_UPLOAD_DIR.resolve(strict=False))
    except ValueError:
        raise HTTPException(status_code=400, detail="Pfad liegt ausserhalb des Uploads-Ordners.")
    return target


def _upload_entry(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "is_dir": p.is_dir(),
        "ingestable": p.is_dir() or p.suffix.lower() in _INGESTABLE_EXTENSIONS,
    }


@app.get("/v1/dashboard/uploads", tags=["Dashboard"])
async def dashboard_uploads(admin: User = Depends(dashboard_admin)):
    if not _UPLOAD_DIR.is_dir():
        return {"dir": str(_UPLOAD_DIR), "files": []}
    entries = []
    for p in sorted(_UPLOAD_DIR.iterdir(), key=lambda x: x.name.lower()):
        try:
            entries.append(_upload_entry(p))
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"dir": str(_UPLOAD_DIR), "files": entries, "max_bytes": _MAX_UPLOAD_BYTES,
            "extensions": sorted(_UPLOAD_EXTENSIONS)}


@app.post("/v1/dashboard/uploads", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_UPLOAD_RATE_LIMIT", "30/minute"))
async def dashboard_upload(request: Request, file: UploadFile = File(...),
                           admin: User = Depends(dashboard_admin),
                           csrf = Depends(check_dashboard_csrf)):
    name = _safe_upload_name(file.filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in _UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=415,
                            detail=f"Dateityp '{suffix or '?'}' ist nicht zugelassen.")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = _upload_path(name)
    #--- Bestehende Datei nicht ueberschreiben, sondern durchnummerieren.
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        for i in range(2, 100):
            target = _upload_path(f"{stem}_{i}{ext}")
            if not target.exists():
                break
        else:
            #--- Alle Nummern belegt: zufaelliger Zusatz statt still name_99 zu
            #--- ueberschreiben.
            target = _upload_path(f"{stem}_{uuid.uuid4().hex[:8]}{ext}")
    #--- In Bloecken schreiben und mitzaehlen: ein content-length-Header ist
    #--- Fremdeingabe, die ankommenden Bytes sind es nicht.
    written = 0
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Datei groesser als {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Schreiben fehlgeschlagen: {e}")
    log.info(f"Upload durch '{admin.email}': {target.name} ({written} Bytes)")
    return {"status": "ok", **_upload_entry(target)}


@app.post("/v1/dashboard/uploads/ingest_all", tags=["Dashboard"])
async def dashboard_upload_ingest_all(admin: User = Depends(dashboard_admin),
                                      csrf = Depends(check_dashboard_csrf)):
    '''Indexiert alle ingestierbaren Dateien im Uploads-Ordner.

    Ruft je Datei denselben Weg wie der Einzel-Endpunkt auf -- eine zweite
    Ingest-Logik waere eine zweite Quelle fuer dieselbe Frage, wie ein Dokument
    zerlegt wird. Nicht ingestierbare Endungen (Bilder) werden uebersprungen,
    nicht als Fehler gemeldet.'''
    if not _UPLOAD_DIR.is_dir():
        return {"status": "ok", "verarbeitet": 0, "indexiert": 0, "ergebnisse": []}
    kandidaten = sorted(
        (p for p in _UPLOAD_DIR.iterdir()
         if p.is_dir() or p.suffix.lower() in _INGESTABLE_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )
    ergebnisse, indexiert = [], 0
    for p in kandidaten:
        try:
            r = await dashboard_upload_ingest(p.name, admin=admin, csrf=csrf)
            indexiert += r.get("indexed", 0)
            ergebnisse.append({"name": p.name, "status": "ok",
                               "indexed": r.get("indexed", 0), "total": r.get("total", 0)})
        except HTTPException as e:
            ergebnisse.append({"name": p.name, "status": "fehler", "detail": str(e.detail)})
    return {"status": "ok", "verarbeitet": len(ergebnisse), "indexiert": indexiert,
            "ergebnisse": ergebnisse}


@app.post("/v1/dashboard/uploads/{name}/ingest", tags=["Dashboard"])
async def dashboard_upload_ingest(name: str, admin: User = Depends(dashboard_admin),
                                  csrf = Depends(check_dashboard_csrf)):
    '''Indexiert eine hochgeladene Datei oder einen entpackten Ordner in Qdrant.

    Nutzt dieselben Bausteine wie der Stapel-Ingest und das ingest_document-Werkzeug
    (load_file zum Lesen, _store_text_in_qdrant zum Chunken und Ablegen) -- damit ein
    ueber das Dashboard indexiertes Dokument identisch zerlegt wird wie eines aus
    documents_to_ingest.'''
    from rag_backend.agent import _store_text_in_qdrant
    from rag_backend.ingest import load_file

    target = _upload_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")

    files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
    if not files:
        raise HTTPException(status_code=400, detail="Der Ordner enthaelt keine Dateien.")

    results, ok_count = [], 0
    for f in files:
        rel = f.relative_to(target) if target.is_dir() else Path(f.name)
        label = str(rel).replace("/", "_").replace("\\", "_")
        try:
            text = await asyncio.to_thread(load_file, f)
            if not text or not text.strip():
                results.append({"file": label, "status": "leer",
                                "message": "Kein Text lesbar (Bild-PDF oder leeres Dokument?)."})
                continue
            msg = await _store_text_in_qdrant(
                label, text, source=f"C:\\Argus_Workspace\\Uploads\\{name}")
            failed = msg.startswith("ERROR")
            results.append({"file": label, "status": "fehler" if failed else "ok",
                            "message": msg})
            if not failed:
                ok_count += 1
        except Exception as e:
            results.append({"file": label, "status": "fehler", "message": str(e)})

    log.info(f"Indexierung durch '{admin.email}': {name} ({ok_count}/{len(files)} erfolgreich)")
    return {"status": "ok", "indexed": ok_count, "total": len(files), "results": results}


@app.delete("/v1/dashboard/uploads/{name}", tags=["Dashboard"])
async def dashboard_upload_delete(name: str, admin: User = Depends(dashboard_admin),
                                  csrf = Depends(check_dashboard_csrf)):
    import shutil

    target = _upload_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")
    #--- Loescht nur die Datei. Indexierte Inhalte raeumt die Dokumentenliste ab.
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "deleted", "name": target.name}


# ---- Benutzer-Dateibereich (fuer alle Rollen, strikt isoliert) ----

@app.get("/v1/dashboard/files", tags=["Dashboard"])
async def dashboard_user_files(user: User = Depends(dashboard_user)):
    from rag_backend import user_files
    dateien = user_files.list_user_files(user.id, "dateien")
    ergebnisse = user_files.list_user_files(user.id, "ergebnisse")
    used_bytes = user_files.get_user_used_bytes(user.id)
    max_bytes = user_files.get_user_files_max_bytes()
    return {
        "dateien": dateien,
        "ergebnisse": ergebnisse,
        "used_bytes": used_bytes,
        "max_bytes": max_bytes,
    }


@app.post("/v1/dashboard/files", tags=["Dashboard"])
@limiter.limit(os.getenv("DASHBOARD_UPLOAD_RATE_LIMIT", "30/minute"))
async def dashboard_user_file_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(dashboard_user),
    csrf = Depends(check_dashboard_csrf),
):
    from rag_backend import user_files

    raw_filename = file.filename or ""
    base_name = os.path.basename(raw_filename.replace("\\", "/")).strip()
    try:
        name = user_files.safe_filename(base_name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unbrauchbarer Dateiname.")

    suffix = Path(name).suffix.lower()
    if suffix not in _UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Dateityp '{suffix or '?'}' ist nicht zugelassen.",
        )

    dateien_dir = user_files.get_user_dir(user.id, "dateien", create=True)
    target = dateien_dir / name
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        for i in range(2, 100):
            cand = dateien_dir / f"{stem}_{i}{ext}"
            if not cand.exists():
                target = cand
                name = target.name
                break
        else:
            target = dateien_dir / f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
            name = target.name

    written = 0
    max_upload = _MAX_UPLOAD_BYTES
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_upload:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Datei groesser als {max_upload // (1024 * 1024)} MB.",
                    )
                try:
                    user_files.check_user_quota(user.id, len(chunk))
                except ValueError:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail="Speicherplatzgrenze fuer dieses Konto ueberschritten.",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Schreiben fehlgeschlagen: {e}")

    st = target.stat()
    log.info(f"Benutzerdatei hochgeladen von '{user.email}' (ID {user.id}): {target.name} ({written} Bytes)")
    return {
        "status": "ok",
        "bereich": "dateien",
        "name": target.name,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@app.get("/v1/dashboard/files/{bereich}/{name}", tags=["Dashboard"])
async def dashboard_user_file_download(
    bereich: str,
    name: str,
    user: User = Depends(dashboard_user),
):
    from rag_backend import user_files

    b_key = (bereich or "").strip().lower()
    if b_key not in ("dateien", "ergebnisse"):
        raise HTTPException(status_code=400, detail="Unbekannter Bereich.")

    try:
        target = user_files.resolve_user_file(user.id, b_key, name, must_exist=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname.")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")

    return FileResponse(
        path=target,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/v1/dashboard/files/{bereich}/{name}", tags=["Dashboard"])
async def dashboard_user_file_delete(
    bereich: str,
    name: str,
    user: User = Depends(dashboard_user),
    csrf = Depends(check_dashboard_csrf),
):
    from rag_backend import user_files

    b_key = (bereich or "").strip().lower()
    if b_key not in ("dateien", "ergebnisse"):
        raise HTTPException(status_code=400, detail="Unbekannter Bereich.")

    try:
        user_files.delete_user_file(user.id, b_key, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname.")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")

    return {"status": "deleted", "bereich": b_key, "name": name}


