import os
os.environ.setdefault("OAI_MODEL", "test-model")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
import time
import pytest
import httpx
from unittest.mock import patch, MagicMock

from rag_backend.crypto_utils import load_llm_api_key, load_secret, encrypt_msg
from rag_backend.agent import _serving_budget, _reset_serving_budget_cache
from rag_backend import metrics
from rag_backend.utils import make_qwen_llm, get_env_first
from rag_backend.fast_path import FAST_PATH_DICT


# ---------------------------------------------------------------------------
# 1. API-Key Tests (crypto_utils, utils)
# ---------------------------------------------------------------------------

def test_load_llm_api_key_new_name_wins(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "new-secret-key")
    monkeypatch.setenv("SGLANG_API_KEY", "old-secret-key")
    assert load_llm_api_key() == "new-secret-key"


def test_load_llm_api_key_fallback_to_old_name(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SGLANG_API_KEY", "old-secret-key")
    assert load_llm_api_key() == "old-secret-key"


def test_load_llm_api_key_enc_decrypted(monkeypatch):
    plain = "my_super_secret_token_1234"
    enc = "ENC:" + encrypt_msg(plain).decode("utf-8")
    monkeypatch.setenv("LLM_API_KEY", enc)
    monkeypatch.delenv("SGLANG_API_KEY", raising=False)
    assert load_llm_api_key() == plain


def test_load_llm_api_key_fallback_enc_decrypted(monkeypatch):
    plain = "fallback_secret_token_5678"
    enc = "ENC:" + encrypt_msg(plain).decode("utf-8")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SGLANG_API_KEY", enc)
    assert load_llm_api_key() == plain


def test_utils_make_qwen_llm_keys_and_base_url(monkeypatch):
    monkeypatch.setenv("OAI_MODEL", "qwen-test")

    # Neuer Name gewinnt
    monkeypatch.setenv("LLM_API_KEY", "key-new")
    monkeypatch.setenv("SGLANG_API_KEY", "key-old")
    monkeypatch.delenv("OAI_BASE_URL", raising=False)
    llm1 = make_qwen_llm(temperature=0.0, top_p=1.0, max_tokens=100)
    assert llm1.openai_api_key.get_secret_value() == "key-new"
    assert str(llm1.openai_api_base).rstrip("/") == "http://llm:8080/v1"

    # Rueckfall auf alten Namen
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    llm2 = make_qwen_llm(temperature=0.0, top_p=1.0, max_tokens=100)
    assert llm2.openai_api_key.get_secret_value() == "key-old"

    # Rueckfall auf "dummy"
    monkeypatch.delenv("SGLANG_API_KEY", raising=False)
    llm3 = make_qwen_llm(temperature=0.0, top_p=1.0, max_tokens=100)
    assert llm3.openai_api_key.get_secret_value() == "dummy"


def test_get_env_first(monkeypatch):
    monkeypatch.setenv("NEW_VAR", "val_new")
    monkeypatch.setenv("OLD_VAR", "val_old")
    assert get_env_first("NEW_VAR", "OLD_VAR", default="fallback") == "val_new"

    monkeypatch.delenv("NEW_VAR", raising=False)
    assert get_env_first("NEW_VAR", "OLD_VAR", default="fallback") == "val_old"

    monkeypatch.delenv("OLD_VAR", raising=False)
    assert get_env_first("NEW_VAR", "OLD_VAR", default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# 2. Context Budget Tests (agent._serving_budget)
# ---------------------------------------------------------------------------

class _MockHttpxResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("HTTP Error", request=None, response=self)


def _clear_budget_env(monkeypatch):
    _reset_serving_budget_cache()
    monkeypatch.delenv("LLM_CONTEXT_LIMIT", raising=False)
    monkeypatch.delenv("SGLANG_MAX_SEQUENCE_LEN", raising=False)
    monkeypatch.delenv("SGLANG_KV_POOL_TOKENS", raising=False)


def test_serving_budget_no_config_measured_72000(monkeypatch):
    """(i) ohne Konfig, gemessen 72000 -> 72000"""
    _clear_budget_env(monkeypatch)

    def fake_get(url, *args, **kwargs):
        if url.endswith("/props"):
            return _MockHttpxResponse(200, {"default_generation_settings": {"n_ctx": 72000}})
        return _MockHttpxResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    budget = _serving_budget()
    assert budget == 72000


def test_serving_budget_llm_context_limit_50000(monkeypatch):
    """(ii) LLM_CONTEXT_LIMIT=50000, gemessen 72000 -> 50000"""
    _clear_budget_env(monkeypatch)
    monkeypatch.setenv("LLM_CONTEXT_LIMIT", "50000")

    def fake_get(url, *args, **kwargs):
        if url.endswith("/props"):
            return _MockHttpxResponse(200, {"default_generation_settings": {"n_ctx": 72000}})
        return _MockHttpxResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    budget = _serving_budget()
    assert budget == 50000


def test_serving_budget_old_name_sglang_max_seq_len_102400(monkeypatch):
    """(iii) nur alte Namen SGLANG_MAX_SEQUENCE_LEN=102400, gemessen 72000 -> 72000"""
    _clear_budget_env(monkeypatch)
    monkeypatch.setenv("SGLANG_MAX_SEQUENCE_LEN", "102400")

    def fake_get(url, *args, **kwargs):
        if url.endswith("/props"):
            return _MockHttpxResponse(200, {"default_generation_settings": {"n_ctx": 72000}})
        return _MockHttpxResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    budget = _serving_budget()
    assert budget == 72000


def test_serving_budget_measurement_failure_retry_after_60s(monkeypatch):
    """(iv) Messung schlaegt fehl -> Konfigwert bzw. 25000, und ein zweiter Aufruf
    nach >60 s (Zeit mocken) misst erneut und uebernimmt dann den gemessenen Wert"""
    _clear_budget_env(monkeypatch)
    monkeypatch.setenv("LLM_CONTEXT_LIMIT", "80000")

    current_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: current_time)

    server_running = False

    def fake_get(url, *args, **kwargs):
        if not server_running:
            raise httpx.ConnectError("Connection refused: LLM still starting")
        if url.endswith("/props"):
            return _MockHttpxResponse(200, {"default_generation_settings": {"n_ctx": 72000}})
        return _MockHttpxResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    # 1. Erster Aufruf waehrend Start: Fehlschlag -> Konfigwert 80000
    b1 = _serving_budget()
    assert b1 == 80000

    # 2. Aufruf nach 30s: Server laeuft inzwischen, aber Cache (< 60s) greift noch
    current_time = 1030.0
    server_running = True
    b2 = _serving_budget()
    assert b2 == 80000

    # 3. Aufruf nach 65s (> 60s): Cache ist abgelaufen, erneute Messung -> min(80000, 72000) = 72000
    current_time = 1065.0
    b3 = _serving_budget()
    assert b3 == 72000

    # 4. Weiterer Aufruf viel spaeter: der gemessene Wert bleibt dauerhaft gecacht
    current_time = 5000.0
    server_running = False  # Selbst wenn Server weg ist, echter Wert bleibt
    b4 = _serving_budget()
    assert b4 == 72000


def test_serving_budget_measurement_failure_default_25000_retry(monkeypatch):
    """(iv-b) Ohne gesetzte Obergrenze: Fehlschlag -> 25000, nach >60s gemessener Wert."""
    _clear_budget_env(monkeypatch)

    current_time = 2000.0
    monkeypatch.setattr(time, "time", lambda: current_time)

    server_running = False

    def fake_get(url, *args, **kwargs):
        if not server_running:
            raise httpx.ConnectError("LLM offline")
        if url.endswith("/props"):
            return _MockHttpxResponse(200, {"default_generation_settings": {"n_ctx": 72000}})
        return _MockHttpxResponse(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    # 1. Fehlschlag ohne Konfig -> 25000
    assert _serving_budget() == 25000

    # 2. Nach 45s (<60s) -> noch 25000
    current_time = 2045.0
    server_running = True
    assert _serving_budget() == 25000

    # 3. Nach 70s (>60s) -> Re-Messung liefert 72000
    current_time = 2070.0
    assert _serving_budget() == 72000


def test_serving_budget_props_404_falls_back_to_get_server_info(monkeypatch):
    """(v) /props liefert 404 -> /get_server_info wird genutzt"""
    _clear_budget_env(monkeypatch)

    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/props"):
            return _MockHttpxResponse(404)
        if url.endswith("/get_server_info"):
            return _MockHttpxResponse(200, {"max_total_num_tokens": 65536})
        return _MockHttpxResponse(500)

    monkeypatch.setattr(httpx, "get", fake_get)
    budget = _serving_budget()
    assert budget == 65536
    assert any("/props" in c for c in calls)
    assert any("/get_server_info" in c for c in calls)


# ---------------------------------------------------------------------------
# 3. Model Metrics Tests (metrics.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llamacpp_metrics_parsing_and_auth_header(monkeypatch):
    """Beispieltext im llama.cpp-Format wird richtig abgebildet, inklusive cache_hit_rate;
    der Authorization-Header wird gesendet."""
    monkeypatch.setenv("LLM_API_KEY", "secret-test-token-llm")
    monkeypatch.setenv("LLM_METRICS_URL", "http://llm:8080/metrics")

    sample_llamacpp_metrics = """# HELP llamacpp:predicted_tokens_seconds Tokens generated per second
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 42.5
# HELP llamacpp:requests_processing Number of requests currently processing
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 2
# HELP llamacpp:requests_deferred Number of requests deferred in queue
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 1
# HELP llamacpp:prompt_tokens_seconds Prompt processing tokens per second
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 1250.0
# HELP llamacpp:prompt_tokens_cached_total Tokens retrieved from KV cache
# TYPE llamacpp:prompt_tokens_cached_total counter
llamacpp:prompt_tokens_cached_total 800.0
# HELP llamacpp:prompt_tokens_total Total prompt tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1000.0
"""

    recorded_headers = {}
    recorded_url = ""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None, **kwargs):
            nonlocal recorded_headers, recorded_url
            recorded_url = url
            recorded_headers = headers or {}
            return _MockHttpxResponse(200, text=sample_llamacpp_metrics)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    res = await metrics.llm_metrics()

    assert recorded_headers.get("Authorization") == "Bearer secret-test-token-llm"
    assert recorded_url == "http://llm:8080/metrics"

    assert res["gen_throughput_tok_s"] == 42.5
    assert res["running_reqs"] == 2.0
    assert res["queue_reqs"] == 1.0
    assert res["prompt_throughput_tok_s"] == 1250.0
    assert res["cache_hit_rate"] == 0.8  # 800 / 1000
    assert "kv_cache_usage" not in res

    # Pruefen, dass sglang_metrics der identische Alias ist
    res_alias = await metrics.sglang_metrics()
    assert res_alias == res


@pytest.mark.asyncio
async def test_sglang_metrics_format_compatibility(monkeypatch):
    """SGLang-Metrikformat bleibt weiterhin parsebar."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SGLANG_API_KEY", "secret-test-token-sglang")

    sample_sglang_metrics = """sglang:gen_throughput 28.5
sglang:num_running_reqs 4
sglang:num_queue_reqs 2
sglang:token_usage 0.52
sglang:cache_hit_rate 0.88
"""

    recorded_headers = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None, **kwargs):
            nonlocal recorded_headers
            recorded_headers = headers or {}
            return _MockHttpxResponse(200, text=sample_sglang_metrics)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    res = await metrics.llm_metrics()
    assert recorded_headers.get("Authorization") == "Bearer secret-test-token-sglang"
    assert res["gen_throughput_tok_s"] == 28.5
    assert res["running_reqs"] == 4.0
    assert res["queue_reqs"] == 2.0
    assert res["kv_cache_usage"] == 0.52
    assert res["cache_hit_rate"] == 0.88


# ---------------------------------------------------------------------------
# 4. Text/Version Tests (fast_path)
# ---------------------------------------------------------------------------

def test_fast_path_mentions_llamacpp():
    assert "llama.cpp" in FAST_PATH_DICT["version"]
    assert "SGLang" not in FAST_PATH_DICT["version"]
    assert "llama.cpp" in FAST_PATH_DICT["status"]
    assert "SGLang" not in FAST_PATH_DICT["status"]


def test_sampling_follows_env_only_for_tunable_instances(monkeypatch):
    #--- Ohne Schalter: die Qwen-Vorgaben.
    for name in ("LLM_TEMPERATURE", "LLM_TOP_P", "LLM_TOP_K"):
        monkeypatch.delenv(name, raising=False)
    llm = make_qwen_llm(temperature=0.7, top_p=0.8, max_tokens=10, tunable=True)
    assert (llm.temperature, llm.top_p, llm.extra_body["top_k"]) == (0.7, 0.8, 20)
    #--- Mit Schaltern (Gemma 4): nur die Antwort-Instanzen folgen Temperatur und
    #--- top_p, top_k gilt fuer alle.
    monkeypatch.setenv("LLM_TEMPERATURE", "1.0")
    monkeypatch.setenv("LLM_TOP_P", "0.95")
    monkeypatch.setenv("LLM_TOP_K", "64")
    llm = make_qwen_llm(temperature=0.7, top_p=0.8, max_tokens=10, tunable=True)
    assert (llm.temperature, llm.top_p, llm.extra_body["top_k"]) == (1.0, 0.95, 64)
    task = make_qwen_llm(temperature=0.1, top_p=0.9, max_tokens=10)
    assert (task.temperature, task.top_p, task.extra_body["top_k"]) == (0.1, 0.9, 64)
    #--- Unlesbare Werte fallen auf die Vorgabe zurueck.
    monkeypatch.setenv("LLM_TEMPERATURE", "warm")
    assert make_qwen_llm(temperature=0.7, top_p=0.8, max_tokens=10, tunable=True).temperature == 0.7
