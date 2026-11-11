"""Leichtgewichtige Metrik-Sammlung fuers Ops-Dashboard.

Zwei Quellen:
- In-Process Ring-Buffer (record_*) fuer Agent-/Request-Metriken (Route, Tools,
  TTFT, Latenz, geschaetzte tok/s). Kein DB-Zugriff im Hot-Path.
- llm_metrics(): scrapt den Prometheus-/metrics-Endpoint des LLM-Servers (braucht
  --metrics bzw. --enable-metrics am Server). Liefert die ECHTEN Engine-Zahlen (Decode-tok/s,
  laufende Requests, KV-Cache-Auslastung / Hit-Rate). Nicht erreichbar -> leeres Dict.
"""
import os
import re
import time
from collections import deque, Counter

import httpx

_MAX = int(os.getenv("DASHBOARD_METRICS_BUFFER", "200"))
_requests: deque = deque(maxlen=_MAX)
_route_counts: Counter = Counter()
_tool_counts: Counter = Counter()
_rag_scores: deque = deque(maxlen=_MAX)
#--- Kompaktierungen sind ein Gesamtzaehler, kein Ring-Buffer (siehe record_compaction).
_compactions_total: int = 0
_last_compaction: dict | None = None

# Char/Token-Schaetzung zentral in utils; als Modul-Attribut gespiegelt (main.py liest metrics._CHARS_PER_TOKEN).
from rag_backend.utils import CHARS_PER_TOKEN as _CHARS_PER_TOKEN


def record_request(*, ttft_ms=None, total_ms=None, completion_chars=0):
    tok_s = None
    if total_ms and completion_chars:
        secs = total_ms / 1000.0
        if secs > 0:
            tok_s = round((completion_chars / _CHARS_PER_TOKEN) / secs, 1)
    _requests.append({
        "ts": time.time(),
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "completion_chars": completion_chars,
        "tok_s": tok_s,
    })


def record_route(route):
    if route:
        _route_counts[route] += 1


def record_tool(name):
    if name:
        _tool_counts[name] += 1


def record_rag_score(score):
    # Top-Reranker-Relevanz pro document_search-Aufruf.
    try:
        _rag_scores.append(float(score))
    except (TypeError, ValueError):
        pass


def record_compaction(*, tokens_before=None, threshold=None, condensed=0):
    """Auto-Kompaktierung der Nachrichtenkette (agent.py) mitschreiben.

    Zaehlt bewusst NICHT ueber den Ring-Buffer: die Kompaktierung ist ein seltenes,
    erklaerungsbeduerftiges Ereignis -- 'seit wann laeuft der Stack, wie oft war der
    Kontext zu voll' soll auch nach 200 Requests noch stimmen. Am KV-Orb im Dashboard
    beantwortet das die Frage, ob eine niedrige KV-Auslastung echt ist oder gerade
    erst durch Verdichtung erkauft wurde."""
    global _compactions_total, _last_compaction
    _compactions_total += 1
    _last_compaction = {
        "ts": time.time(),
        "tokens_before": tokens_before,
        "threshold": threshold,
        "condensed": condensed,
    }


def snapshot():
    reqs = list(_requests)
    recent = reqs[-50:]

    def _avg(key):
        vals = [r[key] for r in recent if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    rag_recent = list(_rag_scores)[-50:]
    avg_rag_score = round(sum(rag_recent) / len(rag_recent), 2) if rag_recent else None

    return {
        "requests_total": len(reqs),
        "avg_tok_s_backend": _avg("tok_s"),
        "avg_ttft_ms": _avg("ttft_ms"),
        "avg_total_ms": _avg("total_ms"),
        "avg_rag_score": avg_rag_score,
        "routes": dict(_route_counts),
        "tools": dict(_tool_counts.most_common(12)),
        "recent": recent[-20:],
        "compactions_total": _compactions_total,
        "last_compaction": _last_compaction,
    }


# Bekannte Metriknamen von SGLang und llama.cpp
_DIRECT_METRICS = {
    # SGLang
    "sglang:gen_throughput": "gen_throughput_tok_s",
    "sglang:num_running_reqs": "running_reqs",
    "sglang:num_queue_reqs": "queue_reqs",
    "sglang:token_usage": "kv_cache_usage",
    "sglang:cache_hit_rate": "cache_hit_rate",
    # llama.cpp
    "llamacpp:predicted_tokens_seconds": "gen_throughput_tok_s",
    "llamacpp:requests_processing": "running_reqs",
    "llamacpp:requests_deferred": "queue_reqs",
    "llamacpp:prompt_tokens_seconds": "prompt_throughput_tok_s",
}

_METRIC_RE = re.compile(r"^([a-zA-Z0-9_:]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$")


async def llm_metrics():
    out = {}
    try:
        from rag_backend.crypto_utils import load_llm_api_key
        from rag_backend.utils import get_env_first
        key = (load_llm_api_key() or "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = get_env_first("LLM_METRICS_URL", "SGLANG_METRICS_URL", default="http://llm:8080/metrics")
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            text = resp.text
    except Exception:
        return out

    raw_vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, val = m.group(1), m.group(2)
        try:
            raw_vals[name] = float(val)
        except ValueError:
            pass

    for metric_name, out_key in _DIRECT_METRICS.items():
        if metric_name in raw_vals:
            out[out_key] = raw_vals[metric_name]

    # llama.cpp berechnet cache_hit_rate aus prompt_tokens_cached_total / prompt_tokens_total
    if "cache_hit_rate" not in out:
        cached = raw_vals.get("llamacpp:prompt_tokens_cached_total")
        total = raw_vals.get("llamacpp:prompt_tokens_total")
        if cached is not None and total is not None and total > 0:
            out["cache_hit_rate"] = cached / total

    return out


# Alias fuer Abwaertskompatibilitaet
sglang_metrics = llm_metrics


_TTS_METRICS_URL = os.getenv("TTS_METRICS_URL", "http://tts-service:8002/v1/tts/metrics")

async def tts_metrics():
    out = {}
    try:
        from rag_backend.crypto_utils import load_llm_api_key
        key = (load_llm_api_key() or "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(_TTS_METRICS_URL, headers=headers)
            if resp.status_code == 200:
                out = resp.json()
    except Exception:
        pass
    return out


_SEARXNG_HEALTH_URL = os.getenv("SEARXNG_HEALTH_URL", "http://searxng:8080/")
_SEARXNG_TTL = float(os.getenv("SEARXNG_HEALTH_TTL", "15"))
_searxng_cache = {"ts": 0.0, "online": False}


async def searxng_online():
    # Echter Health-Check des SearxNG-Containers, gecacht (TTL).
    now = time.time()
    if now - _searxng_cache["ts"] < _SEARXNG_TTL:
        return _searxng_cache["online"]
    online = False
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(_SEARXNG_HEALTH_URL)
            online = resp.status_code < 500
    except Exception:
        online = False
    _searxng_cache["ts"] = now
    _searxng_cache["online"] = online
    return online


def load_historical_metrics(db_session):
    from datetime import timezone
    import logging

    from rag_backend.crypto_utils import decrypt_msg
    from rag_backend.models import Message

    try:
        messages = (
            db_session.query(Message)
            .filter(Message.sender == "assistant")
            .filter(Message.latency_ms.isnot(None))
            .order_by(Message.timestamp.desc())
            .limit(100)
            .all()
        )
        _requests.clear()

        for msg in reversed(messages):
            try:
                decrypted = decrypt_msg(msg.content_encrypted)
                completion_chars = len(decrypted)
            except Exception:
                completion_chars = 0

            _requests.append({
                "ts": msg.timestamp.replace(tzinfo=timezone.utc).timestamp() if msg.timestamp else time.time(),
                "ttft_ms": msg.ttft_ms,
                "total_ms": msg.latency_ms,
                "completion_chars": completion_chars,
                "tok_s": msg.tokens_per_sec,
            })
        logging.getLogger(__name__).info(f"Loaded {len(messages)} historical metrics from the database.")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to load historical metrics: {e}")
