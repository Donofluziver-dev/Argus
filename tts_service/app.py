import asyncio
import hashlib
import io
import os
import re
import secrets
import subprocess
import tempfile
import wave
import time
from collections import deque, OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.fernet import Fernet
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

# --- Konfiguration (alle via docker-compose/.env ueberschreibbar) ---
QWEN_TALKER = os.getenv("QWEN_TALKER", "/app/models/qwen-talker-1.7b-customvoice-Q8_0.gguf")
QWEN_BASE_TALKER = os.getenv("QWEN_BASE_TALKER", "/app/models/qwen-talker-1.7b-base-Q8_0.gguf")
QWEN_CODEC = os.getenv("QWEN_CODEC", "/app/models/qwen-tokenizer-12hz-Q8_0.gguf")
QWEN_LANG = os.getenv("QWEN_LANG", "auto")
QWEN_VOICE = os.getenv("QWEN_VOICE", "").strip()
# Pflicht-Fallback fuer den Server-Modus: das CustomVoice-Modell braucht
# immer einen Sprecher.
QWEN_DEFAULT_VOICE = os.getenv("QWEN_DEFAULT_VOICE", "ryan").strip() or "ryan"
QWEN_SERVER_PORT = int(os.getenv("QWEN_SERVER_PORT", "8080"))
# Gueltige Preset-Sprecher. Open WebUI schickt OpenAI-Stimmnamen ->
# im Preset-Modus auf einen bekannten Sprecher normalisieren.
QWEN_KNOWN_VOICES = {"serena", "vivian", "uncle_fu", "ryan", "aiden",
                     "ono_anna", "sohee", "eric", "dylan"}

# Clone-Modus (WARM): sind Referenz-Dateien gesetzt, laedt der tts-server
# sie einmalig beim Start und nutzt sie fuer alle Anfragen.
# Schneller ohne Per-Call-Encode: vorab erzeugte .spk + .rvq statt WAV.
QWEN_CLONE_REF_WAV = os.getenv("QWEN_CLONE_REF_WAV", "").strip()
QWEN_CLONE_REF_TEXT = os.getenv("QWEN_CLONE_REF_TEXT", "").strip()
QWEN_CLONE_REF_SPK = os.getenv("QWEN_CLONE_REF_SPK", "").strip()
QWEN_CLONE_REF_RVQ = os.getenv("QWEN_CLONE_REF_RVQ", "").strip()
# Fester Seed ueber alle Anfragen gegen Stimm-Drift. Leer = Upstream
# (Hardware-Random pro Satz). Empfehlung: QWEN_SEED setzen, QWEN_TEMP leer.
QWEN_SEED = os.getenv("QWEN_SEED", "").strip()
QWEN_TEMP = os.getenv("QWEN_TEMP", "").strip()

def _ref_present(p: str) -> bool:
    return bool(p) and Path(p).is_file()

# Clone nur aktiv, wenn die Referenzdatei wirklich da ist -- sonst startet
# der Server im Preset-Modus statt zu sterben.
CLONE_CONFIGURED = bool(QWEN_CLONE_REF_WAV or QWEN_CLONE_REF_SPK)
CLONE_MODE = _ref_present(QWEN_CLONE_REF_WAV) or _ref_present(QWEN_CLONE_REF_SPK)

MAX_INPUT_CHARS = int(os.getenv("TTS_MAX_INPUT_CHARS", "4000"))
SAMPLE_RATE = 24000
_synth_lock = asyncio.Lock()
_server_proc = None
_http = None

# --- Markdown/Code aus dem Text fuer die Sprachausgabe entfernen ---
# ZENTRALE Bereinigung fuer ALLE Voice-Clients: Quellen-Trailer,
# Markdown-Links -> Linktext, Zahlenbereiche "16-17" -> "16 bis 17".
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_URL_RE = re.compile(r"https?://\S+")
_MD_HEAD_RE = re.compile(r"^#+\s*", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*]\s+", re.MULTILINE)
_MD_BOLD_ITALIC_RE = re.compile(r"\*+|_+")
_MULTI_SPACE_RE = re.compile(r"\s+")
# Quellen-/URL-Trailer am Antwortende ("Verwendete Quellen: ...") nicht vorlesen.
_SOURCES_TRAILER_RE = re.compile(
    r"(?is)\n\s*(?:verwendete\s+quellen|verwendete\s+urls|quellenangaben|quellen|sources)\s*:.*"
)
# Markdown-Link [Text](url) -> nur den Linktext behalten.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]*\)")
# Zahlenbereiche ausschreiben; uebrige Striche -> Leerzeichen.
_NUM_RANGE_RE = re.compile(r"(\d)\s*[-\u2013\u2014]\s*(\d)")
_DASH_RE = re.compile(r"[-\u2013\u2014]")

def _strip_for_tts(text: str) -> str:
    t = _SOURCES_TRAILER_RE.sub("", text)
    t = _CODE_BLOCK_RE.sub(" [Codeblock] ", t)
    t = _INLINE_CODE_RE.sub(" ", t)
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub(" ", t)
    t = _MD_HEAD_RE.sub("", t)
    t = _MD_BULLET_RE.sub("", t)
    t = _MD_BOLD_ITALIC_RE.sub("", t)
    t = _NUM_RANGE_RE.sub(r"\1 bis \2", t)
    t = _DASH_RE.sub(" ", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    return t.strip()


class SpeechRequest(BaseModel):
    input: str
    voice: str | None = None
    # Default mp3 fuer Browser-Clients; ogg nur auf ausdrueckliche Anfrage.
    response_format: str = "mp3"
    # Optionaler Client-Kurz-Cut, NACH _strip_for_tts satzweise angewendet.
    max_chars: int | None = None


async def _wait_for_server(timeout: float = 180.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    url = f"http://127.0.0.1:{QWEN_SERVER_PORT}/v1/audio/speech"
    async with httpx.AsyncClient(timeout=30.0) as probe:
        while loop.time() < deadline:
            if _server_proc is not None and _server_proc.poll() is not None:
                raise RuntimeError("tts-server-Prozess vorzeitig beendet (siehe Container-Log).")
            try:
                # Warmup-Probe: sobald der Server HTTP antwortet, ist das Modell geladen.
                await probe.post(url, json={"input": "."})
                return
            except Exception:
                await asyncio.sleep(1.5)
    raise RuntimeError("tts-server nicht rechtzeitig bereit.")


_API_KEY_PATH = Path("/run/secrets/llm_api_key")
_FERNET_KEY_PATH = Path("/run/secrets/fernet_key")
_api_key: str | None = None


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


@asynccontextmanager
async def lifespan(app):
    global _api_key, _server_proc, _http
    _api_key = _load_api_key()
    _http = httpx.AsyncClient(timeout=120.0)
    if CLONE_CONFIGURED and not CLONE_MODE:
        print("[tts] WARNUNG: Clone konfiguriert, aber Referenzdatei fehlt "
              "-> Preset-Modus. voice_ref/ pruefen.", flush=True)
    # tts-server immer resident starten -> Modell bleibt geladen (warm, beide Modi).
    cmd = ["tts-server", "--codec", QWEN_CODEC,
           "--host", "127.0.0.1", "--port", str(QWEN_SERVER_PORT), "--lang", QWEN_LANG]
    if CLONE_MODE:
        # Base-Modell + Referenzstimme beim Start (Argus-Patch laedt sie einmalig).
        cmd += ["--model", QWEN_BASE_TALKER]
        if QWEN_CLONE_REF_WAV:
            cmd += ["--ref-wav", QWEN_CLONE_REF_WAV]
        if QWEN_CLONE_REF_SPK:
            cmd += ["--ref-spk", QWEN_CLONE_REF_SPK]
        if QWEN_CLONE_REF_RVQ:
            cmd += ["--ref-rvq", QWEN_CLONE_REF_RVQ]
        if QWEN_CLONE_REF_TEXT:
            cmd += ["--ref-text", QWEN_CLONE_REF_TEXT]
    else:
        cmd += ["--model", QWEN_TALKER]
    # Feste Sampling-Parameter (beide Modi) -> stabile Stimme ueber alle Saetze.
    if QWEN_SEED:
        cmd += ["--seed", QWEN_SEED]
    if QWEN_TEMP:
        cmd += ["--temp", QWEN_TEMP]
    _server_proc = subprocess.Popen(cmd)
    await _wait_for_server()
    yield
    _api_key = None
    if _server_proc is not None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=10)
        except Exception:
            _server_proc.kill()
    if _http is not None:
        await _http.aclose()


app = FastAPI(title="Argus TTS (Qwen3-TTS / qwentts.cpp)", lifespan=lifespan)


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


def _to_wav(audio: bytes) -> bytes:
    if audio[:4] == b"RIFF":
        return audio
    # rohes PCM (s16le 24k mono) in einen WAV-Container packen
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio)
    return buf.getvalue()


def _audio_to_ogg(audio: bytes) -> bytes:
    # qwentts liefert je nach Build WAV (RIFF) oder rohes PCM -> beides abfangen.
    is_wav = audio[:4] == b"RIFF"
    in_args = ["-i", "pipe:0"] if is_wav else ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *in_args,
           "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1"]
    proc = subprocess.run(cmd, input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg ogg-Konvertierung fehlgeschlagen: {proc.stderr.decode('utf-8','ignore')[:300]}")
    return proc.stdout


def _audio_to_mp3(audio: bytes) -> bytes:
    # Browser-taugliches Format (Open WebUI). qwentts liefert WAV (RIFF) oder rohes PCM.
    is_wav = audio[:4] == b"RIFF"
    in_args = ["-i", "pipe:0"] if is_wav else ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *in_args,
           "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"]
    proc = subprocess.run(cmd, input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg mp3-Konvertierung fehlgeschlagen: {proc.stderr.decode('utf-8','ignore')[:300]}")
    return proc.stdout


async def _synth_server(text: str, voice: str | None) -> bytes:
    payload = {"input": text}
    if not CLONE_MODE:
        # CustomVoice VERLANGT einen gueltigen Sprecher, sonst liefert der
        # Server stumme 0 Bytes. Im Clone-Modus wird voice ignoriert.
        req_voice = (voice or "").strip().lower()
        if req_voice not in QWEN_KNOWN_VOICES:
            env_voice = QWEN_VOICE.strip().lower()
            req_voice = env_voice if env_voice in QWEN_KNOWN_VOICES else QWEN_DEFAULT_VOICE
        payload["voice"] = req_voice
    resp = await _http.post(f"http://127.0.0.1:{QWEN_SERVER_PORT}/v1/audio/speech", json=payload)
    resp.raise_for_status()
    return resp.content


def _get_audio_duration(audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0
    if audio_bytes[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception:
            return max(0.0, (len(audio_bytes) - 44) / 48000.0)
    return len(audio_bytes) / 48000.0


_MAX_TTS_HISTORY = 100
_tts_requests = deque(maxlen=_MAX_TTS_HISTORY)
_tts_requests_total = 0

# --- Audio-Cache ---
# Eine Synthese kostet ungefaehr so viel Zeit, wie das Ergebnis spaeter
# dauert (RTF um 1). Ein zweites "Vorlesen" desselben Absatzes darf das
# nicht noch einmal bezahlen -- gleicher Text, gleiche Stimme, gleiches
# Format ergeben Byte fuer Byte dasselbe Audio. Schluessel ist ein Hash,
# nicht der Text selbst: der Cache liegt im Speicher und soll keine
# vorgelesenen Vertragsinhalte im Klartext vorhalten.
_CACHE_MAX_ENTRIES = int(os.getenv("TTS_CACHE_MAX_ENTRIES", "64"))
_CACHE_MAX_BYTES = int(float(os.getenv("TTS_CACHE_MAX_MB", "256")) * 1024 * 1024)
_tts_cache = OrderedDict()
_tts_cache_bytes = 0
_tts_cache_hits = 0


def _cache_key(text: str, voice: str | None, fmt: str) -> str:
    # Feldweise gehasht statt zu einem String verkettet: so kann kein
    # Textinhalt die Feldgrenzen verschieben.
    h = hashlib.sha256()
    for teil in (text, (voice or "").strip().lower(), fmt, QWEN_CLONE_REF_WAV,
                 QWEN_CLONE_REF_SPK, QWEN_SEED, QWEN_TEMP):
        h.update(str(teil).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def _cache_get(key: str):
    global _tts_cache_hits
    hit = _tts_cache.get(key)
    if hit is None:
        return None
    _tts_cache.move_to_end(key)
    _tts_cache_hits += 1
    return hit


def _cache_put(key: str, content: bytes, media_type: str) -> None:
    global _tts_cache_bytes
    size = len(content)
    if _CACHE_MAX_ENTRIES <= 0 or size > _CACHE_MAX_BYTES:
        return  # Cache aus, oder ein einzelner Eintrag sprengt das Budget.
    if key in _tts_cache:
        _tts_cache_bytes -= len(_tts_cache[key][0])
    _tts_cache[key] = (content, media_type)
    _tts_cache.move_to_end(key)
    _tts_cache_bytes += size
    while _tts_cache and (len(_tts_cache) > _CACHE_MAX_ENTRIES
                          or _tts_cache_bytes > _CACHE_MAX_BYTES):
        _, (alt, _mt) = _tts_cache.popitem(last=False)
        _tts_cache_bytes -= len(alt)


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    global _tts_requests_total
    text = _strip_for_tts(req.input or "")
    if not text:
        raise HTTPException(status_code=400, detail="Leerer Input nach Bereinigung.")
    if req.max_chars and req.max_chars > 0 and len(text) > req.max_chars:
        # An Satzgrenze kuerzen, solange mehr als die Haelfte erhalten bleibt.
        truncated = text[:req.max_chars]
        boundary = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
        if boundary > req.max_chars // 2:
            text = text[:boundary + 1]
        else:
            text = truncated.rstrip() + "..."
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
    # Unbekannte Formate fallen auf mp3 zurueck statt 400.
    fmt = (req.response_format or "mp3").lower()
    key = _cache_key(text, req.voice, fmt)
    cached = _cache_get(key)
    if cached is not None:
        # Bewusst VOR dem Lock: ein Treffer wartet nicht auf eine laufende
        # Synthese. Und bewusst ohne Metrik-Eintrag -- eine Auslieferung in
        # Millisekunden wuerde den RTF-Schnitt verfaelschen.
        return Response(content=cached[0], media_type=cached[1])
    t0 = time.perf_counter()
    async with _synth_lock:
        # Zweite Pruefung im Lock: warteten zwei Anfragen auf denselben
        # Text, hat die erste ihn inzwischen erzeugt.
        cached = _cache_get(key)
        if cached is not None:
            return Response(content=cached[0], media_type=cached[1])
        try:
            audio = await _synth_server(text, req.voice)
            if fmt == "wav":
                res_content = _to_wav(audio)
                media_type = "audio/wav"
            elif fmt == "ogg":
                res_content = await asyncio.to_thread(_audio_to_ogg, audio)
                media_type = "audio/ogg"
            else:
                res_content = await asyncio.to_thread(_audio_to_mp3, audio)
                media_type = "audio/mpeg"

            t1 = time.perf_counter()
            latency_s = t1 - t0
            chars = len(text)
            audio_duration_s = _get_audio_duration(audio)
            rtf = latency_s / audio_duration_s if audio_duration_s > 0 else 0.0
            _tts_requests_total += 1
            _tts_requests.append({
                "ts": time.time(),
                "latency_s": latency_s,
                "chars": chars,
                "chars_per_sec": chars / latency_s if latency_s > 0 else 0,
                "audio_duration_s": audio_duration_s,
                "rtf": rtf
            })
            _cache_put(key, res_content, media_type)
            return Response(content=res_content, media_type=media_type)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"TTS-Fehler: {e}")


@app.get("/v1/tts/metrics")
async def tts_metrics():
    reqs = list(_tts_requests)
    avg_latency = sum(r["latency_s"] for r in reqs) / len(reqs) if reqs else 0.0
    avg_chars_per_sec = sum(r["chars_per_sec"] for r in reqs) / len(reqs) if reqs else 0.0
    avg_rtf = sum(r["rtf"] for r in reqs) / len(reqs) if reqs else 0.0
    return {
        "tts_requests_total": _tts_requests_total,
        "cache_hits": _tts_cache_hits,
        "cache_entries": len(_tts_cache),
        "cache_mb": round(_tts_cache_bytes / 1024 / 1024, 1),
        "avg_latency_s": round(avg_latency, 3),
        "avg_chars_per_sec": round(avg_chars_per_sec, 1),
        "avg_rtf": round(avg_rtf, 4),
        "recent": reqs[-10:],
        "mode": "clone" if CLONE_MODE else "server",
        "voice": "clone" if CLONE_MODE else (QWEN_VOICE or QWEN_DEFAULT_VOICE),
        "lang": QWEN_LANG,
    }


@app.get("/healthz")
async def health():
    # Offen fuer den Healthcheck: Details liegen geschuetzt unter /v1/tts/metrics.
    alive = _server_proc is not None and _server_proc.poll() is None
    if not alive:
        raise HTTPException(status_code=503, detail="tts-server laeuft nicht.")
    return {"status": "ok"}
