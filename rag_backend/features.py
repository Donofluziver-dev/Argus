"""Zustand und Pruefung optionaler Compose-Profile (voice, tracing, web).

Der Anfragepfad loest nie selbst per DNS auf. Solange die Hintergrundaufgabe
noch nicht geprueft hat (z. B. in Tests ohne lifespan), gilt alles als
vorhanden (Standard: True).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any, Callable

log = logging.getLogger(__name__)

# Standard vor der ersten Pruefung: alles vorhanden (heutiges Verhalten)
_OPTIONAL_SERVICES: dict[str, bool] = {
    "voice": True,
    "tracing": True,
    "web": True,
}
_OPTIONAL_SERVICES_REFRESH_SECONDS = 30


def _service_hostname(url_or_host: str, default: str = "") -> str:
    raw = (url_or_host or "").strip() or default
    if not raw:
        return ""
    if "://" in raw:
        from urllib.parse import urlsplit
        return (urlsplit(raw).hostname or "").strip()
    return raw.split("/")[0].split(":")[0].strip()


async def _can_resolve_host(host: str) -> bool:
    h = (host or "").strip()
    if not h:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, h, None, 0, socket.SOCK_STREAM)
        return bool(infos)
    except OSError:
        return False


async def _check_optional_services_once() -> dict[str, bool]:
    stt_host = _service_hostname(os.getenv("STT_SERVICE_URL", "http://stt-service:8003"), "stt-service")
    tts_host = _service_hostname(os.getenv("TTS_SERVICE_URL", "http://tts-service:8002"), "tts-service")
    phoenix_ep = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006").strip()
    phoenix_host = _service_hostname(phoenix_ep) if phoenix_ep else ""
    searxng_url = os.getenv("SEARXNG_URL", os.getenv("SEARXNG_HEALTH_URL", "http://searxng:8080/")).strip()
    searxng_host = _service_hostname(searxng_url, "searxng")

    # Parallel: Ein Name, den es nicht gibt, kostet bei Dockers DNS rund 4 s.
    stt_ok, tts_ok, tracing_ok, web_ok = await asyncio.gather(
        _can_resolve_host(stt_host),
        _can_resolve_host(tts_host),
        _can_resolve_host(phoenix_host),
        _can_resolve_host(searxng_host),
    )
    return {
        "voice": stt_ok and tts_ok,
        "tracing": tracing_ok,
        "web": web_ok,
    }


async def _optional_services_watcher(on_tracing_available: Callable[[], Any] | None = None):
    """Prueft alle 30 Sekunden im Hintergrund per DNS, ob optionale Dienste aufloesbar sind."""
    global _OPTIONAL_SERVICES
    while True:
        try:
            _OPTIONAL_SERVICES = await _check_optional_services_once()
            if _OPTIONAL_SERVICES.get("tracing") and on_tracing_available is not None:
                res = on_tracing_available()
                if asyncio.iscoroutine(res):
                    await res
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"Optionale Dienste nicht pruefbar: {e}")
        await asyncio.sleep(_OPTIONAL_SERVICES_REFRESH_SECONDS)


def is_voice_available() -> bool:
    return bool(_OPTIONAL_SERVICES.get("voice", True))


def is_tracing_available() -> bool:
    return bool(_OPTIONAL_SERVICES.get("tracing", True))


def is_web_available() -> bool:
    return bool(_OPTIONAL_SERVICES.get("web", True))


def current_features() -> dict[str, bool]:
    return {
        "voice": is_voice_available(),
        "tracing": is_tracing_available(),
        "web": is_web_available(),
    }
