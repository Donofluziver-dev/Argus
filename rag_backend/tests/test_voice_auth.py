"""Tests fuer die API-Key-Absicherung von stt-service und tts-service sowie
die Aufrufer in rag_backend (Dashboard, Metriken).

Laeuft ohne faster-whisper, ffmpeg oder tts-server und ist sowohl mit pytest
als auch direkt ueber unittest ausfuehrbar.
"""
import asyncio
import importlib.util
import logging
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
STT_APP_PATH = REPO_ROOT / "stt_service" / "app.py"
TTS_APP_PATH = REPO_ROOT / "tts_service" / "app.py"


def _load_service_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_encrypted_secret(folder: Path, plain_api_key: str) -> tuple[Path, Path, bytes]:
    fernet_key = Fernet.generate_key()
    cipher = "ENC:" + Fernet(fernet_key).encrypt(plain_api_key.encode("utf-8")).decode("utf-8")
    f_path = folder / "fernet_key"
    s_path = folder / "llm_api_key"
    f_path.write_bytes(fernet_key + b"\n")
    s_path.write_text(cipher + "\n", encoding="utf-8")
    return s_path, f_path, fernet_key


@unittest.skipUnless(STT_APP_PATH.is_file() and TTS_APP_PATH.is_file(),
                     "stt_service/tts_service ausserhalb des Test-Kontexts nicht vorhanden")
class TestVoiceServiceAuth(unittest.TestCase):

    def test_stt_decrypts_once_and_guards_all_routes_except_healthz(self):
        stt = _load_service_module("stt_service_test_mod", STT_APP_PATH)
        api_key = "test-stt-secret-key-987654"

        with tempfile.TemporaryDirectory() as tmp:
            s_path, f_path, _ = _write_encrypted_secret(Path(tmp), api_key)

            async def _fake_load_model():
                stt._model = object()
                return stt._model

            def _fake_transcribe(path: str, language: str) -> dict:
                return {"text": "Hallo Welt", "language": language or "de", "duration": 1.0, "segments": []}

            with patch.object(stt, "_API_KEY_PATH", s_path), \
                 patch.object(stt, "_FERNET_KEY_PATH", f_path), \
                 patch.object(stt, "_load_model", side_effect=_fake_load_model), \
                 patch.object(stt, "_transcribe_sync", side_effect=_fake_transcribe):
                with TestClient(stt.app) as client:
                    # Nach dem Start Dateien entfernen: der Schluessel liegt nur im Speicher.
                    s_path.unlink()
                    f_path.unlink()

                    # /healthz bleibt ohne Schluessel erreichbar und enthaelt keine Interna.
                    h = client.get("/healthz")
                    self.assertEqual(h.status_code, 200)
                    self.assertEqual(h.json(), {"status": "ok"})

                    # Fehlender, leerer oder falscher Header -> 401 auf allen anderen Routen.
                    files = {"file": ("probe.ogg", b"OggS-dummy", "audio/ogg")}
                    self.assertEqual(client.post("/v1/audio/transcriptions", files=files).status_code, 401)
                    self.assertEqual(
                        client.post("/v1/audio/transcriptions", files=files,
                                    headers={"Authorization": "Bearer falsch"}).status_code,
                        401,
                    )
                    self.assertEqual(
                        client.post("/v1/audio/transcriptions", files=files,
                                    headers={"Authorization": f"Basic {api_key}"}).status_code,
                        401,
                    )
                    self.assertEqual(
                        client.post("/v1/audio/transcriptions", files=files,
                                    headers={"Authorization": api_key}).status_code,
                        401,
                    )
                    self.assertEqual(
                        client.post("/v1/audio/transcriptions", files=files,
                                    headers={"Authorization": "Bearer "}).status_code,
                        401,
                    )
                    # Nicht-ASCII im Header (über Socket als Latin-1 empfangen) -> 401 statt 500.
                    self.assertFalse(stt._authorized("Bearer äöü"))
                    self.assertEqual(
                        client.post("/v1/audio/transcriptions", files=files,
                                    headers=[(b"authorization", b"Bearer \xe4\xf6\xfc")]).status_code,
                        401,
                    )
                    self.assertEqual(client.get("/openapi.json").status_code, 401)
                    self.assertEqual(client.get("/docs").status_code, 401)

                    # Richtiger Schluessel wird per secrets.compare_digest geprueft.
                    with patch.object(stt.secrets, "compare_digest", wraps=secrets.compare_digest) as spy:
                        ok = client.post(
                            "/v1/audio/transcriptions",
                            files=files,
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                        self.assertEqual(ok.status_code, 200)
                        self.assertEqual(ok.json()["text"], "Hallo Welt")
                        self.assertGreaterEqual(spy.call_count, 1)

    def test_stt_fail_closed_without_secret_leak(self):
        stt = _load_service_module("stt_service_fail_mod", STT_APP_PATH)
        sensitive_value = "geheimer-klartext-schluessel-42"

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            s_path = folder / "llm_api_key"
            f_path = folder / "fernet_key"
            model_called = False

            async def _unexpected_load():
                nonlocal model_called
                model_called = True

            # 1. Fehlende Datei -> Start bricht vor dem Modell-Laden ab.
            with patch.object(stt, "_API_KEY_PATH", s_path), \
                 patch.object(stt, "_FERNET_KEY_PATH", f_path), \
                 patch.object(stt, "_load_model", side_effect=_unexpected_load):
                with self.assertRaises(RuntimeError):
                    with TestClient(stt.app):
                        pass
                self.assertFalse(model_called)

            # 2. Ungueltiger Fernet-Ciphertext -> kein Schluessel-/Token-Inhalt im Fehlertext.
            bad_cipher = f"ENC:{sensitive_value}"
            f_key = Fernet.generate_key()
            s_path.write_text(bad_cipher, encoding="utf-8")
            f_path.write_bytes(f_key)
            with patch.object(stt, "_API_KEY_PATH", s_path), \
                 patch.object(stt, "_FERNET_KEY_PATH", f_path), \
                 patch.object(stt, "_load_model", side_effect=_unexpected_load):
                with self.assertRaises(RuntimeError) as ctx:
                    with TestClient(stt.app):
                        pass
                msg = str(ctx.exception)
                self.assertNotIn(sensitive_value, msg)
                self.assertNotIn(f_key.decode("utf-8"), msg)
                self.assertFalse(model_called)

            # 3. Leerer Schluessel -> ebenfalls fail-closed.
            s_path.write_text("   \n", encoding="utf-8")
            with patch.object(stt, "_API_KEY_PATH", s_path), \
                 patch.object(stt, "_FERNET_KEY_PATH", f_path), \
                 patch.object(stt, "_load_model", side_effect=_unexpected_load):
                with self.assertRaises(RuntimeError):
                    with TestClient(stt.app):
                        pass

    def test_tts_decrypts_once_and_guards_all_routes_except_healthz(self):
        tts = _load_service_module("tts_service_test_mod", TTS_APP_PATH)
        api_key = "test-tts-secret-key-123456"

        class _FakeProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        async def _noop_wait(*_a, **_kw):
            return None

        async def _fake_synth(_text: str, _voice: str | None) -> bytes:
            # Kurzes PCM (s16le): wav-Pfad braucht kein ffmpeg.
            return b"\x00\x00" * 2400

        with tempfile.TemporaryDirectory() as tmp:
            s_path, f_path, _ = _write_encrypted_secret(Path(tmp), api_key)

            with patch.object(tts, "_API_KEY_PATH", s_path), \
                 patch.object(tts, "_FERNET_KEY_PATH", f_path), \
                 patch.object(tts.subprocess, "Popen", return_value=_FakeProc()), \
                 patch.object(tts, "_wait_for_server", side_effect=_noop_wait), \
                 patch.object(tts, "_synth_server", side_effect=_fake_synth):
                with TestClient(tts.app) as client:
                    # Nach dem Start Dateien loeschen: Schluessel bleibt nur im Speicher.
                    s_path.unlink()
                    f_path.unlink()

                    # /healthz offen und ohne Konfigurationsdetails.
                    h = client.get("/healthz")
                    self.assertEqual(h.status_code, 200)
                    self.assertEqual(h.json(), {"status": "ok"})

                    # Alle uebrigen Routen verlangen Bearer-Key.
                    self.assertEqual(client.get("/v1/tts/metrics").status_code, 401)
                    self.assertEqual(
                        client.get("/v1/tts/metrics",
                                   headers={"Authorization": "Bearer falsch"}).status_code,
                        401,
                    )
                    self.assertEqual(
                        client.post("/v1/audio/speech", json={"input": "Hallo"}).status_code,
                        401,
                    )
                    self.assertFalse(tts._authorized("Bearer äöü"))
                    self.assertEqual(
                        client.post("/v1/audio/speech", json={"input": "Hallo"},
                                    headers=[(b"authorization", b"Bearer \xe4\xf6\xfc")]).status_code,
                        401,
                    )
                    self.assertEqual(client.get("/openapi.json").status_code, 401)
                    self.assertEqual(client.get("/docs").status_code, 401)

                    auth = {"Authorization": f"Bearer {api_key}"}
                    with patch.object(tts.secrets, "compare_digest", wraps=secrets.compare_digest) as spy:
                        m = client.get("/v1/tts/metrics", headers=auth)
                        self.assertEqual(m.status_code, 200)
                        self.assertIn("tts_requests_total", m.json())

                        sp = client.post(
                            "/v1/audio/speech",
                            headers=auth,
                            json={"input": "Hallo Welt", "response_format": "wav"},
                        )
                        self.assertEqual(sp.status_code, 200)
                        self.assertEqual(sp.headers["content-type"], "audio/wav")
                        self.assertTrue(sp.content.startswith(b"RIFF"))
                        self.assertGreaterEqual(spy.call_count, 2)

    def test_tts_fail_closed_without_spawning_server_or_leaking_secret(self):
        tts = _load_service_module("tts_service_fail_mod", TTS_APP_PATH)
        sensitive_value = "geheimer-tts-schluessel-99"

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            s_path = folder / "llm_api_key"
            f_path = folder / "fernet_key"
            f_key = Fernet.generate_key()
            s_path.write_text(f"ENC:{sensitive_value}", encoding="utf-8")
            # fernet_key fehlt absichtlich -> Entschluesselung scheitert.

            with patch.object(tts, "_API_KEY_PATH", s_path), \
                 patch.object(tts, "_FERNET_KEY_PATH", f_path), \
                 patch.object(tts.subprocess, "Popen") as popen_spy:
                with self.assertRaises(RuntimeError) as ctx:
                    with TestClient(tts.app):
                        pass
                msg = str(ctx.exception)
                self.assertNotIn(sensitive_value, msg)
                self.assertNotIn(f_key.decode("utf-8"), msg)
                popen_spy.assert_not_called()


class TestRagBackendVoiceCallers(unittest.TestCase):

    def _encrypted_env_key(self, plain: str) -> str:
        fernet_key = os.environ["FERNET_KEY"].encode("utf-8")
        return "ENC:" + Fernet(fernet_key).encrypt(plain.encode("utf-8")).decode("utf-8")

    def test_metrics_tts_sends_bearer_key(self):
        from rag_backend import crypto_utils, metrics

        seen = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"tts_requests_total": 3}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, **kw):
                seen["url"] = url
                seen["headers"] = headers
                return _FakeResp()

        enc = self._encrypted_env_key("shared-llm-key-metrics")
        old_fernet = crypto_utils._fernet_instance
        crypto_utils._fernet_instance = None
        try:
            with patch.dict(os.environ, {"SGLANG_API_KEY": enc}), \
                 patch.object(metrics.httpx, "AsyncClient", _FakeClient):
                out = asyncio.run(metrics.tts_metrics())
        finally:
            crypto_utils._fernet_instance = old_fernet

        self.assertEqual(out, {"tts_requests_total": 3})
        self.assertEqual(seen["headers"], {"Authorization": "Bearer shared-llm-key-metrics"})


try:
    import pytest as _pytest  # noqa: F401
    from rag_backend.tests.test_main import (  # noqa: F401
        DASH_ORIGIN,
        _make_account,
        override_get_db,
        test_client,
    )
    _HAS_MAIN_FIXTURES = True
except ImportError:
    _HAS_MAIN_FIXTURES = False


if _HAS_MAIN_FIXTURES:
    def test_dashboard_stt_and_tts_forward_bearer_key(client, monkeypatch):
        import httpx
        from rag_backend import crypto_utils

        _make_account(client, "chef", "einLangesPasswort", "admin")
        monkeypatch.setattr(crypto_utils, "_fernet_instance", None)
        enc = "ENC:" + Fernet(os.environ["FERNET_KEY"].encode("utf-8")).encrypt(
            b"shared-llm-key-dashboard"
        ).decode("utf-8")
        monkeypatch.setenv("SGLANG_API_KEY", enc)

        seen_calls = []

        class _FakeResp:
            def __init__(self, url: str):
                self.status_code = 200
                self.content = b"RIFF-audio"
                self.headers = {"content-type": "audio/mpeg"}
                self._url = url

            def raise_for_status(self):
                return None

            def json(self):
                return {"text": "Transkript aus Dashboard"}

        class _FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kwargs):
                seen_calls.append((url, kwargs))
                return _FakeResp(url)

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

        r_stt = client.post(
            "/v1/dashboard/stt",
            headers=DASH_ORIGIN,
            files={"file": ("aufnahme.webm", b"webm-bytes", "audio/webm")},
        )
        assert r_stt.status_code == 200, r_stt.text
        assert r_stt.json() == {"text": "Transkript aus Dashboard"}

        r_tts = client.post(
            "/v1/dashboard/tts",
            headers=DASH_ORIGIN,
            json={"input": "Antwort vorlesen", "voice": "ryan"},
        )
        assert r_tts.status_code == 200, r_tts.text
        assert r_tts.content == b"RIFF-audio"

        assert len(seen_calls) == 2
        assert seen_calls[0][0].endswith("/v1/audio/transcriptions")
        assert seen_calls[0][1].get("headers") == {"Authorization": "Bearer shared-llm-key-dashboard"}
        assert seen_calls[1][0].endswith("/v1/audio/speech")
        assert seen_calls[1][1].get("headers") == {"Authorization": "Bearer shared-llm-key-dashboard"}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
