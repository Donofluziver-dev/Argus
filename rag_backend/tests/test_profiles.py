"""Tests fuer Auftrag D: Sprache und Ablaufverfolgung als abschaltbare Compose-Profile."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("OAI_MODEL", "test-model")

import pytest

from rag_backend import main as backend
from rag_backend import metrics
from rag_backend.tests.test_main import (  # noqa: F401
    DASH_ORIGIN,
    _make_account,
    override_get_db,
    test_client,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(backend.__file__).resolve().parent / "static"


def _load_setup_module(name: str, filename: str):
    path = REPO_ROOT / "setup" / filename
    if not path.exists():
        pytest.skip(f"setup/{filename} nicht eingebunden")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# 1. Watcher-Standardzustand, DNS-Aufloesung und /v1/dashboard/auth/state
# =============================================================================


def test_watcher_default_state_is_present():
    """Solange die Hintergrundaufgabe noch nicht gelaufen ist (z. B. in Tests
    ohne lifespan), gelten Sprache, Ablaufverfolgung und Websuche als vorhanden."""
    from rag_backend import features
    assert features._OPTIONAL_SERVICES == {"voice": True, "tracing": True, "web": True}
    assert backend.is_voice_available() is True
    assert backend.is_tracing_available() is True
    assert backend.is_web_available() is True


@pytest.mark.asyncio
async def test_optional_services_watcher_resolves_via_getaddrinfo(monkeypatch):
    seen_hosts: list[str] = []

    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        seen_hosts.append(host)
        if host in ("stt-service", "tts-service"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.10", 0))]
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("STT_SERVICE_URL", "http://stt-service:8003")
    monkeypatch.setenv("TTS_SERVICE_URL", "http://tts-service:8002")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006")
    monkeypatch.setenv("SEARXNG_URL", "http://searxng:8080")

    from rag_backend import features
    state = await features._check_optional_services_once()
    assert state == {"voice": True, "tracing": False, "web": False}
    assert {"stt-service", "tts-service", "phoenix", "searxng"} <= set(seen_hosts)

    # Fehlt einer der beiden Sprachdienste, gilt Sprache als nicht vorhanden.
    def fake_getaddrinfo_no_tts(host, port, *args, **kwargs):
        if host in ("stt-service", "searxng"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.10", 0))]
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo_no_tts)
    state2 = await features._check_optional_services_once()
    assert state2 == {"voice": False, "tracing": False, "web": True}


def test_auth_state_reports_features_without_leaking_addresses(client, monkeypatch):
    from rag_backend import features
    # Standard: alle drei vorhanden
    r = client.get("/v1/dashboard/auth/state")
    assert r.status_code == 200
    body = r.json()
    assert body["features"] == {"voice": True, "tracing": True, "web": True}
    # Keine internen Hostnamen oder Ports in /auth/state
    for secret_or_host in ("stt-service", "tts-service", "phoenix", "searxng", "6006", "8002", "8003", "8080"):
        assert secret_or_host not in r.text

    # Abgeschaltet: features spiegeln den Watcher-Zustand
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": False, "tracing": False, "web": False})
    r_off = client.get("/v1/dashboard/auth/state")
    assert r_off.status_code == 200
    assert r_off.json()["features"] == {"voice": False, "tracing": False, "web": False}

    # Auch angemeldet bleibt features enthalten
    _make_account(client, "chef", "einLangesPasswort", "admin")
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": False, "web": False})
    r_auth = client.get("/v1/dashboard/auth/state")
    assert r_auth.status_code == 200
    assert r_auth.json()["authenticated"] is True
    assert r_auth.json()["features"] == {"voice": True, "tracing": False, "web": False}


# =============================================================================
# 2. /v1/dashboard/stt und /v1/dashboard/tts bei fehlender Sprache (503 vs 502)
# =============================================================================


def test_stt_and_tts_return_503_without_voice_and_never_call_service(client, monkeypatch):
    import httpx

    from rag_backend import features
    _make_account(client, "chef", "einLangesPasswort", "admin")
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": False, "tracing": True, "web": True})

    called = []

    class ForbiddenClient:
        def __init__(self, *a, **kw):
            called.append("init")
            raise AssertionError("httpx.AsyncClient darf ohne Sprache nicht aufgerufen werden")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)

    r_stt = client.post(
        "/v1/dashboard/stt",
        headers=DASH_ORIGIN,
        files={"file": ("probe.webm", b"audio", "audio/webm")},
    )
    assert r_stt.status_code == 503
    assert r_stt.json()["detail"] == "Sprache ist auf diesem Server nicht eingerichtet."

    r_tts = client.post(
        "/v1/dashboard/tts",
        headers=DASH_ORIGIN,
        json={"input": "Hallo Welt"},
    )
    assert r_tts.status_code == 503
    assert r_tts.json()["detail"] == "Sprache ist auf diesem Server nicht eingerichtet."
    assert called == []


def test_stt_and_tts_keep_502_when_voice_enabled_but_service_down(client, monkeypatch):
    import httpx

    from rag_backend import features
    _make_account(client, "chef", "einLangesPasswort", "admin")
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": True, "tracing": True, "web": True})

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)

    r_stt = client.post(
        "/v1/dashboard/stt",
        headers=DASH_ORIGIN,
        files={"file": ("probe.webm", b"audio", "audio/webm")},
    )
    assert r_stt.status_code == 502
    assert r_stt.json()["detail"] == "STT-Dienst nicht erreichbar."

    r_tts = client.post(
        "/v1/dashboard/tts",
        headers=DASH_ORIGIN,
        json={"input": "Hallo Welt"},
    )
    assert r_tts.status_code == 502
    assert r_tts.json()["detail"] == "TTS-Dienst nicht erreichbar."


# =============================================================================
# 3. _init_tracing() und metrics.tts_metrics()
# =============================================================================


def test_init_tracing_skips_exporter_when_phoenix_host_unresolvable(monkeypatch, caplog):
    import rag_backend.trace_redaction as tr

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006")

    def fail_resolve(host, *args, **kwargs):
        raise socket.gaierror("Host phoenix not found")

    called = []
    monkeypatch.setattr(socket, "getaddrinfo", fail_resolve)
    monkeypatch.setattr(tr, "build_phoenix_tracer_provider", lambda *a, **kw: called.append(a))

    with caplog.at_level(logging.INFO, logger=backend.log.name):
        backend._init_tracing()

    assert called == []
    info_msgs = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.INFO]
    assert any("Ablaufverfolgung aus:" in m and "Profil tracing" in m for m in info_msgs), info_msgs


@pytest.mark.asyncio
async def test_watcher_sets_up_tracing_when_phoenix_appears_later(monkeypatch):
    """Profil tracing erst nach dem Backend-Start eingeschaltet: Der Watcher richtet die
    Ablaufverfolgung ein, sobald Phoenix aufloesbar ist -- genau einmal."""
    from rag_backend import features
    calls = []

    async def fake_check():
        return {"voice": False, "tracing": True, "web": False}

    def fake_init():
        calls.append(1)
        backend._TRACING_ACTIVE = True
        return True

    monkeypatch.setattr(features, "_check_optional_services_once", fake_check)
    monkeypatch.setattr(backend, "_init_tracing", fake_init)
    monkeypatch.setattr(backend, "_TRACING_ACTIVE", False)
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", dict(features._OPTIONAL_SERVICES))
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES_REFRESH_SECONDS", 0.01)

    #--- So startet lifespan den Watcher.
    task = asyncio.create_task(
        features._optional_services_watcher(on_tracing_available=backend._tracing_nachholen))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [1]
    assert features._OPTIONAL_SERVICES == {"voice": False, "tracing": True, "web": False}


@pytest.mark.asyncio
async def test_metrics_tts_skips_http_and_reports_off_when_voice_disabled(monkeypatch, caplog):
    from rag_backend import features
    monkeypatch.setattr(features, "_OPTIONAL_SERVICES", {"voice": False, "tracing": True, "web": True})

    class ForbiddenClient:
        def __init__(self, *a, **kw):
            raise AssertionError("httpx.AsyncClient darf bei abgeschalteter Sprache nicht genutzt werden")

    monkeypatch.setattr(metrics.httpx, "AsyncClient", ForbiddenClient)

    with caplog.at_level(logging.WARNING):
        res = await metrics.tts_metrics()

    assert res == {"status": "aus"}
    assert caplog.records == []


# =============================================================================
# 4. Netzpruefung (setup/check_networks.py)
# =============================================================================


def test_check_networks_optional_services_and_core_services():
    net_mod = _load_setup_module("check_networks", "check_networks.py")

    def make_cfg(include_optional: bool = True) -> dict:
        core_intern = ("llm", "embed", "rerank", "postgres", "qdrant")
        opt_intern = ("phoenix", "tts-service", "stt-service") if include_optional else ()
        services = {name: {"networks": {"intern": None}} for name in (*core_intern, *opt_intern)}
        services["calc-sandbox"] = {"networks": {"sandbox": None}}
        services["searxng"] = {"networks": {"backend": None}}
        services["local-proxy"] = {
            "networks": {"backend": None},
            "ports": [{"host_ip": "127.0.0.1", "target": 8888, "published": "8888"}],
        }
        services["intern-proxy"] = {
            "networks": {"intern": None, "publish": None},
            "ports": [{"host_ip": "127.0.0.1", "target": 6006, "published": "6006"}],
        }
        services["rag-backend"] = {
            "networks": {"backend": None, "intern": None, "sandbox": None, "edge": None},
            "ports": [{"host_ip": "127.0.0.1", "target": 7860, "published": "7860"}],
        }
        return {
            "networks": {
                "backend": {},
                "intern": {"internal": True},
                "publish": {},
                "sandbox": {"internal": True},
                "edge": {},
            },
            "services": services,
        }

    # 1. Ohne phoenix, tts-service, stt-service -> kein Fehler
    assert net_mod.check(make_cfg(include_optional=False)) == []

    # 2. Mit phoenix, tts-service, stt-service im Netz 'intern' -> kein Fehler
    assert net_mod.check(make_cfg(include_optional=True)) == []

    # 3. Optionaler Dienst im falschen Netz (mit Ausgang) -> Fehler
    cfg_bad_opt = make_cfg(include_optional=True)
    cfg_bad_opt["services"]["phoenix"]["networks"] = {"backend": None}
    errs_opt = net_mod.check(cfg_bad_opt)
    assert any("phoenix" in e for e in errs_opt), errs_opt

    # 4. Fehlender Kerndienst (z. B. postgres oder llm) bleibt Fehler
    for core_name in ("llm", "postgres", "qdrant", "embed", "rerank"):
        cfg_missing_core = make_cfg(include_optional=False)
        del cfg_missing_core["services"][core_name]
        errs_core = net_mod.check(cfg_missing_core)
        assert any(core_name in e and "fehlt" in e for e in errs_core), (core_name, errs_core)


# =============================================================================
# 5. setup/init_stack.py: COMPOSE_PROFILES beim Update erganzen, Vorhandenes behalten
# =============================================================================


def test_init_stack_sets_compose_profiles_only_when_key_missing(tmp_path, monkeypatch):
    init_stack = _load_setup_module("init_stack", "init_stack.py")
    monkeypatch.setattr(init_stack, "restrict_to_owner", lambda _d: False)
    monkeypatch.setattr(init_stack.ensure_stack_secrets, "__defaults__", (lambda _d: False,))

    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    shutil.copy2(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    ws = tmp_path / "ws"
    models = tmp_path / "models"

    # 1. Bestehende .env OHNE Schluessel COMPOSE_PROFILES -> wird mit voice,tracing,web nachgetragen (ARGUS_ENV_VERSION=2)
    (tmp_path / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n",
        encoding="utf-8",
    )
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env1 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env1["COMPOSE_PROFILES"] == "voice,tracing,web"
    assert env1["ARGUS_ENV_VERSION"] == "2"

    # 2. Bestehende .env mit LEEREM COMPOSE_PROFILES= und alter Version -> Migration auf v2 haengt web an
    (tmp_path / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "COMPOSE_PROFILES=\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n",
        encoding="utf-8",
    )
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env2 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env2["COMPOSE_PROFILES"] == "web"
    assert env2["ARGUS_ENV_VERSION"] == "2"

    # 2b. Ist die Version schon 2 und COMPOSE_PROFILES leer: bleibt leer
    (tmp_path / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "ARGUS_ENV_VERSION=2\n"
        "COMPOSE_PROFILES=\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n",
        encoding="utf-8",
    )
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env2b = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env2b["COMPOSE_PROFILES"] == ""

    # 3. Bestehende .env mit eigenem Wert (z. B. proxy) und alter Version -> bekommt web
    (tmp_path / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "COMPOSE_PROFILES=proxy\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n",
        encoding="utf-8",
    )
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env3 = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env3["COMPOSE_PROFILES"] == "proxy,web"
    assert env3["ARGUS_ENV_VERSION"] == "2"

    # 3b. Bestehende .env mit Version 2 und proxy -> proxy bleibt unberuehrt
    (tmp_path / ".env").write_text(
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        "ARGUS_ENV_VERSION=2\n"
        "COMPOSE_PROFILES=proxy\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n",
        encoding="utf-8",
    )
    assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
    env3b = init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env3b["COMPOSE_PROFILES"] == "proxy"


# =============================================================================
# 6. setup/install.py: _merge_compose_profiles, Schalter, Fragen und doctor
# =============================================================================


def test_merge_compose_profiles_add_and_remove():
    installer = _load_setup_module("install", "install.py")
    assert installer._merge_compose_profiles("", "proxy") == "proxy"
    assert installer._merge_compose_profiles("voice,tracing", "proxy") == "voice,tracing,proxy"
    assert installer._merge_compose_profiles(
        "voice,tracing,proxy", {"voice": False, "tracing": True}
    ) == "tracing,proxy"
    assert installer._merge_compose_profiles(
        "proxy", add=["voice"], remove=["tracing"]
    ) == "proxy,voice"
    assert installer._merge_compose_profiles(
        "voice,tracing", remove=["voice", "tracing"]
    ) == ""


def test_installer_voice_and_tracing_switches_and_prompts(tmp_path, monkeypatch, capsys):
    installer = _load_setup_module("install", "install.py")
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "setup" / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(REPO_ROOT / ".env.example", repo / ".env.example")
    shutil.copy2(REPO_ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    init_mod = installer._load_init_stack_module(repo)

    ws = tmp_path / "ws"
    models = tmp_path / "models"

    monkeypatch.setattr(installer, "check_docker_environment", lambda non_interactive=False: None)
    monkeypatch.setattr(installer, "check_gpu", lambda: (True, "RTX 5070 Ti", 16376))
    monkeypatch.setattr(installer, "check_ram_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(installer, "ensure_setup_venv", lambda root: Path("python"))
    monkeypatch.setattr(
        installer,
        "run_init_stack",
        lambda root, py, workspace, models: init_mod.write_env_file(
            root / ".env",
            (root / ".env.example").read_text(encoding="utf-8"),
            {"ARGUS_WORKSPACE_DIR": workspace, "ARGUS_MODELS_DIR": models},
        ),
    )
    monkeypatch.setattr(
        installer.Path,
        "resolve",
        lambda self, *a, **kw: repo / "setup" / "install.py"
        if self == Path(installer.__file__)
        else os.path.abspath(self),
    )
    monkeypatch.setattr(installer, "__file__", str(repo / "setup" / "install.py"))
    monkeypatch.setattr(installer.webbrowser, "open", lambda url: True)

    # 1. --no-voice --no-tracing --no-web mit --server-url (Proxy bleibt erhalten, voice/tracing/web entfernt)
    rc1 = installer.main([
        "--yes", "--no-models", "--no-start",
        "--no-voice", "--no-tracing", "--no-web",
        "--workspace", str(ws), "--models", str(models),
        "--server-url", "https://argus.example.org",
    ])
    assert rc1 == 0
    env1 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env1["COMPOSE_PROFILES"] == "proxy"

    # 2. --voice --no-tracing --no-web auf bestehender .env mit proxy -> proxy,voice
    rc2 = installer.main([
        "--yes", "--no-models", "--no-start",
        "--voice", "--no-tracing", "--no-web",
        "--workspace", str(ws), "--models", str(models),
    ])
    assert rc2 == 0
    env2 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env2["COMPOSE_PROFILES"] == "proxy,voice"

    # 3. 4,4-GB-Hinweis nur mit Profil voice (ohne --no-start, Start-Frage mit 'n' beantwortet)
    # Eingaben: 1 = lokal, Stimme = n, Tracing = j, Web = n, Start = n, Token-Zwischenablage = n, Browser = n
    capsys.readouterr()
    answers_no_voice = iter(["1", "n", "j", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers_no_voice))
    (repo / ".env").unlink()
    rc3 = installer.main([
        "--no-models",
        "--workspace", str(ws), "--models", str(models),
    ])
    assert rc3 == 0
    out_no_voice = capsys.readouterr().out
    assert "der Sprachdienst laedt dabei 4,4 GB" not in out_no_voice
    env3 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env3["COMPOSE_PROFILES"] == "tracing"

    # Eingaben: 1 = lokal, Stimme = j, Tracing = n, Web = n, Start = n, Token-Zwischenablage = n, Browser = n
    answers_with_voice = iter(["1", "j", "n", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers_with_voice))
    rc4 = installer.main([
        "--no-models",
        "--workspace", str(ws), "--models", str(models),
    ])
    assert rc4 == 0
    out_with_voice = capsys.readouterr().out
    assert "der Sprachdienst laedt dabei 4,4 GB" in out_with_voice
    env4 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env4["COMPOSE_PROFILES"] == "voice"


def test_doctor_profiles_and_stack_checks(tmp_path, monkeypatch):
    installer = _load_setup_module("install", "install.py")

    # 1. doctor_check_profiles: fehlender Schluessel -> WARNUNG mit naechstem Schritt
    r_missing = installer.doctor_check_profiles({})
    assert r_missing.status == "WARNUNG"
    assert r_missing.name == "Profile"
    assert "COMPOSE_PROFILES" in r_missing.message
    assert "init_stack.py" in r_missing.message

    # 2. doctor_check_profiles: leerer Wert -> OK (bewusst aus)
    r_empty = installer.doctor_check_profiles({"COMPOSE_PROFILES": ""})
    assert r_empty.status == "OK"

    # 3. doctor_check_profiles: aktive Profile -> OK mit Aufzaehlung
    r_active = installer.doctor_check_profiles({"COMPOSE_PROFILES": "voice,tracing"})
    assert r_active.status == "OK"
    assert "voice, tracing" in r_active.message

    # 4. doctor_check_stack: fehlende oder gestoppte Sprach-/Phoenix-Container sind KEIN Fehler,
    #    wenn ihr Profil aus ist.
    class FakePs:
        returncode = 0
        stdout = (
            '[{"Service": "rag-backend", "State": "running", "Health": "healthy"},'
            ' {"Service": "stt-service", "State": "exited", "Health": ""},'
            ' {"Service": "phoenix", "State": "exited", "Health": ""}]'
        )
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakePs())
    monkeypatch.setattr(installer, "check_healthz", lambda url="": True)

    r_stack_off = installer.doctor_check_stack(tmp_path, env_data={"COMPOSE_PROFILES": ""})
    assert r_stack_off.status == "OK", r_stack_off.message

    # 5. Ist das Profil dagegen aktiv und der Container fehlt/steht, meldet doctor FEHLER.
    r_stack_on = installer.doctor_check_stack(tmp_path, env_data={"COMPOSE_PROFILES": "voice"})
    assert r_stack_on.status == "FEHLER"
    assert "tts-service" in r_stack_on.message or "stt-service" in r_stack_on.message


# =============================================================================
# 7. Statische Oberflaeche: Ausblenden von Sprache und Hinweis bei Traces
# =============================================================================


def test_static_ui_handles_disabled_voice_and_tracing():
    auth_js = (STATIC_DIR / "js" / "auth.js").read_text(encoding="utf-8")
    chat_js = (STATIC_DIR / "js" / "chat.js").read_text(encoding="utf-8")
    ops_js = (STATIC_DIR / "js" / "ops.js").read_text(encoding="utf-8")

    # auth.js blendet #vc-mic, #vc-tts und .chat-action-tts aus (hidden / display:none)
    assert "argusApplyFeatures" in auth_js
    assert "vc-mic" in auth_js and "vc-tts" in auth_js
    assert "chat-action-tts" in auth_js and "chat-action-tts" in chat_js
    # ops.js zeigt ohne Sprache "aus" bei sys-tts und blendet den Trace-Rahmen aus
    assert 'setz("sys-tts", "aus")' in ops_js
    assert "Profil tracing" in ops_js
