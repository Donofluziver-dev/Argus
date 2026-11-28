import asyncio
import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)

TTS_APP_PATH = Path(__file__).resolve().parents[2] / "tts_service" / "app.py"


@pytest.fixture(scope="module")
def tts():
    spec = importlib.util.spec_from_file_location("tts_limits_mod", TTS_APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tts_client(tts, monkeypatch):
    """TTS-Dienst ohne Modellprozess und ohne Schluessel-Pruefung."""
    async def _fake_synth(_text, _voice):
        return b"\x00\x00" * 2400

    monkeypatch.setattr(tts, "_synth_server", _fake_synth)
    monkeypatch.setattr(tts, "_authorized", lambda _h: True)
    monkeypatch.setattr(tts, "_waiting", 0)
    tts._tts_cache.clear()
    return TestClient(tts.app)


# ---- Bereinigung in linearer Zeit --------------------------------------------

@pytest.mark.parametrize("bauart", [
    "\n" * 200000, " " * 200000, "[" * 200000, "[a](" * 50000, "`" * 200000,
    "\nquellen" * 25000, "1" + " " * 200000,
])
def test_cleanup_is_linear(tts, bauart):
    t0 = time.perf_counter()
    tts._strip_for_tts(bauart)
    assert time.perf_counter() - t0 < 1.0


def test_cleanup_still_does_its_job(tts):
    text = "# Titel\n- **Punkt** 16-17 Stueck\n[Link](https://a.b/c)\n\nVerwendete Quellen: [1] x"
    assert tts._strip_for_tts(text) == "Titel Punkt 16 bis 17 Stueck Link"


# ---- Grenzen am Dienst --------------------------------------------------------

def test_raw_text_is_cut_before_cleanup(tts, tts_client, monkeypatch):
    seen = []
    real = tts._strip_for_tts
    monkeypatch.setattr(tts, "_strip_for_tts", lambda t: seen.append(len(t)) or real(t))
    r = tts_client.post("/v1/audio/speech", json={"input": "Wort " * 20000, "response_format": "wav"})
    assert r.status_code == 200
    assert seen == [tts.RAW_MAX_CHARS]


def test_absurd_request_is_rejected_unprocessed(tts, tts_client):
    r = tts_client.post("/v1/audio/speech", json={"input": "a" * (tts.REQUEST_MAX_CHARS + 1)})
    assert r.status_code == 422


def test_full_queue_answers_503_at_once(tts, tts_client, monkeypatch):
    monkeypatch.setattr(tts, "_waiting", tts.MAX_QUEUE)
    r = tts_client.post("/v1/audio/speech", json={"input": "Hallo", "response_format": "wav"})
    assert r.status_code == 503


def test_queue_counter_returns_to_zero(tts, tts_client):
    for _ in range(3):
        r = tts_client.post("/v1/audio/speech", json={"input": "Hallo Welt", "response_format": "wav"})
        assert r.status_code == 200
    assert tts._waiting == 0


def test_synthesis_is_dropped_when_client_is_gone(tts):
    class _Gone:
        async def is_disconnected(self):
            return True

    started = []

    async def _slow():
        started.append(True)
        await asyncio.sleep(30)

    async def _run():
        with pytest.raises(tts._ClientGone):
            await tts._unless_client_gone(_Gone(), _slow())

    t0 = time.perf_counter()
    asyncio.run(_run())
    assert time.perf_counter() - t0 < 2.0


def test_cache_is_separated_per_account_and_not_bypassed_by_voice_names(tts):
    a = tts._cache_key("Text", "alloy", "mp3", "konto-a")
    b = tts._cache_key("Text", "alloy", "mp3", "konto-b")
    assert a != b
    #--- Unbekannte Stimmnamen landen beim selben Sprecher, also beim selben Eintrag.
    assert tts._cache_key("Text", "alloy", "mp3", "x") == tts._cache_key("Text", "nova", "mp3", "x")


# ---- Grenzen im Backend ------------------------------------------------------

@pytest.fixture
def voice_on(monkeypatch):
    from rag_backend import main
    monkeypatch.setattr(main, "is_voice_available", lambda: True)
    main._TTS_USAGE.clear()
    main._TTS_ACTIVE.clear()
    yield main
    main._TTS_USAGE.clear()
    main._TTS_ACTIVE.clear()


def test_backend_rejects_too_long_text(client, voice_on):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    r = client.post("/v1/dashboard/tts", headers=DASH_ORIGIN,
                    json={"input": "a" * (voice_on._TTS_MAX_CHARS + 1)})
    assert r.status_code == 413


def test_backend_limits_parallel_requests_per_account(client, voice_on):
    _make_account(client, "chef", "einLangesPasswort", "admin")
    uid = client.get("/v1/dashboard/auth/state").json()["user_id"]
    voice_on._TTS_ACTIVE[uid] = voice_on._TTS_PARALLEL
    r = client.post("/v1/dashboard/tts", headers=DASH_ORIGIN, json={"input": "Hallo"})
    assert r.status_code == 429


def test_backend_character_budget_per_minute(voice_on, monkeypatch):
    monkeypatch.setattr(voice_on, "_TTS_CHARS_PER_MINUTE", 1000)
    assert voice_on._tts_budget_take(7, 600) is True
    assert voice_on._tts_budget_take(7, 600) is False
    assert voice_on._tts_budget_take(8, 600) is True


def test_cache_scope_differs_per_account_and_hides_the_id(voice_on):
    a, b = voice_on._tts_cache_scope(1), voice_on._tts_cache_scope(2)
    assert a != b and "1" != a and len(a) == 32
