import asyncio
import os
import secrets
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

MODEL_NAME = os.getenv("WHISPER_MODEL", "large-v3-turbo")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "de")
MAX_UPLOAD_MB = 25

_API_KEY_PATH = Path("/run/secrets/llm_api_key")
_FERNET_KEY_PATH = Path("/run/secrets/fernet_key")
_api_key: str | None = None

_model = None
_model_lock = asyncio.Lock()


def _load_api_key(
    secret_path: Path | None = None,
    fernet_path: Path | None = None,
) -> str:
    # Schluessel einmalig beim Start entschluesseln; kein Klartext in Fehlermeldungen (fail-closed).
    s_path = secret_path or _API_KEY_PATH
    f_path = fernet_path or _FERNET_KEY_PATH
    value = ""
    fernet_key = b""
    try:
        value = s_path.read_text(encoding="utf-8").strip()
        if value.startswith("ENC:"):
            fernet_key = f_path.read_bytes().strip()
            value = Fernet(fernet_key).decrypt(value[4:].encode("utf-8")).decode("utf-8").strip()
    except Exception as exc:
        value = ""
        fernet_key = b""
        raise RuntimeError(f"API-Key nicht ladbar ({type(exc).__name__}).") from None
    finally:
        fernet_key = b""
    if not value:
        raise RuntimeError("API-Key ist leer.")
    return value


def _authorized(auth_header: str | None) -> bool:
    if not _api_key or not auth_header:
        return False
    scheme, sep, token = auth_header.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token:
        return False
    return secrets.compare_digest(token.encode("utf-8"), _api_key.encode("utf-8"))


async def _load_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel
        _model = await asyncio.to_thread(
            WhisperModel, MODEL_NAME, device="cpu", compute_type=COMPUTE_TYPE,
        )
    return _model

@asynccontextmanager
async def lifespan(app):
    global _api_key
    _api_key = _load_api_key()
    await _load_model()
    yield
    _api_key = None

app = FastAPI(title="Argus STT (faster-whisper)", lifespan=lifespan)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # Nur /healthz bleibt fuer den Container-Healthcheck ohne Schluessel offen.
    if request.url.path == "/healthz":
        return await call_next(request)
    if not _authorized(request.headers.get("authorization")):
        return JSONResponse(
            status_code=401,
            content={"detail": "Nicht autorisiert."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def _transcribe_sync(path: str, language: str) -> dict:
    segments_iter, info = _model.transcribe(
        path, language=language, beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    text_parts = []
    segments_out = []
    for seg in segments_iter:
        text_parts.append(seg.text)
        segments_out.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })
    return {
        "text": " ".join(p.strip() for p in text_parts).strip(),
        "language": info.language,
        "duration": round(info.duration, 2),
        "segments": segments_out,
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), language: str = Form(None)):
    data = await file.read()
    size_mb = len(data) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"Datei zu gross ({size_mb:.1f} MB > {MAX_UPLOAD_MB} MB).")
    if not data:
        raise HTTPException(status_code=400, detail="Leerer Upload.")
    await _load_model()
    lang = (language or DEFAULT_LANGUAGE).strip() or None
    if lang and lang.lower() in ("auto", "none"):
        lang = None
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "audio.ogg").suffix or ".ogg", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = await asyncio.to_thread(_transcribe_sync, tmp_path, lang)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT-Fehler: {e}")
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

@app.get("/healthz")
async def health():
    # Offen fuer den Healthcheck: nur Bereitschaft, keine Modell- oder Konfigurationsdaten.
    return {"status": "loading" if _model is None else "ok"}
