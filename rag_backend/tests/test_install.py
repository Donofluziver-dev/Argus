"""Tests fuer die gefuehrte Installation (setup/install.py)."""
from __future__ import annotations

import collections
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "setup" / "install.py"
_MODELS_JSON = Path(__file__).resolve().parents[2] / "setup" / "models.json"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="setup/install.py nicht eingebunden")


@pytest.fixture(scope="module")
def installer():
    spec = importlib.util.spec_from_file_location("install", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# 1. Pruefung der models.json
# =============================================================================

def test_models_json_structure_and_constraints():
    assert _MODELS_JSON.exists(), "setup/models.json fehlt"
    data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    models = data.get("models", [])
    assert len(models) >= 1, "Keine Modelle in models.json"

    targets = []
    for model in models:
        assert isinstance(model.get("id"), str) and model["id"]
        assert isinstance(model.get("title"), str) and model["title"]
        assert isinstance(model.get("repo"), str) and model["repo"]
        # Revision: genau 40 Hex-Zeichen
        rev = model.get("revision", "")
        assert re.fullmatch(r"[0-9a-f]{40}", rev), f"Ungueltige Revision: {rev}"

        files = model.get("files", [])
        assert len(files) >= 1, f"Keine Dateien fuer Modell {model['id']}"

        for f in files:
            assert isinstance(f.get("path"), str) and f["path"]
            target = f.get("target")
            assert isinstance(target, str) and target
            # Relativer Pfad, kein fuehrender Slash, kein '..'
            target_p = Path(target)
            assert not target_p.is_absolute()
            assert not target.startswith("/") and not target.startswith("\\")
            assert ".." not in target_p.parts, f"'..' nicht erlaubt im Target: {target}"

            # Groesse > 0
            size = f.get("size")
            assert isinstance(size, int) and size > 0, f"Ungueltige Groesse: {size}"

            # SHA-256: genau 64 Hex-Zeichen
            sha = f.get("sha256", "")
            assert re.fullmatch(r"[0-9a-f]{64}", sha), f"Ungueltige SHA-256: {sha}"

            targets.append(target)

    # Keine doppelten Zielpfade
    assert len(targets) == len(set(targets)), f"Doppelte Zielpfade in models.json: {targets}"


# =============================================================================
# 2. Lokaler HTTP-Server fuer Download-Tests
# =============================================================================

class MockRangeServer:
    def __init__(self, data: bytes):
        self.data = data
        self.ignore_range = False
        self.corrupt_data = False
        self.last_headers: dict[str, str] = {}

        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parent.last_headers = dict(self.headers)
                content = parent.data
                if parent.corrupt_data:
                    content = b"X" * len(parent.data)

                range_header = self.headers.get("Range")
                if range_header and not parent.ignore_range:
                    m = re.match(r"bytes=(\d+)-", range_header)
                    if m:
                        start = int(m.group(1))
                        if start < len(content):
                            chunk = content[start:]
                            self.send_response(206)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("Content-Range", f"bytes {start}-{len(content)-1}/{len(content)}")
                            self.send_header("Content-Length", str(len(chunk)))
                            self.end_headers()
                            self.wfile.write(chunk)
                            return

                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_download_full_file(installer, tmp_path):
    data = b"Das ist ein vollstaendiger Test-Inhalt fuer Argus-Modelle." * 200
    server = MockRangeServer(data)
    try:
        url = f"http://127.0.0.1:{server.port}/model.gguf"
        target = tmp_path / "downloaded_model.gguf"
        sha256 = hashlib.sha256(data).hexdigest()

        installer.download_file_with_resume(
            url=url,
            target_path=target,
            expected_size=len(data),
            expected_sha256=sha256,
            max_retries=2,
            timeout=5.0,
            show_progress=False,
        )

        assert target.exists()
        assert target.read_bytes() == data
        assert not target.with_name(target.name + ".part").exists()
        assert server.last_headers.get("User-Agent") == "argus-setup"
    finally:
        server.shutdown()


def test_download_resume_range_206(installer, tmp_path):
    data = b"0123456789ABCDEF" * 500
    server = MockRangeServer(data)
    try:
        url = f"http://127.0.0.1:{server.port}/resumable_model.gguf"
        target = tmp_path / "resumable_model.gguf"
        part = target.with_name(target.name + ".part")

        # Teil-Datei mit ersten 2000 Bytes vorbereiten
        partial_len = 2000
        part.write_bytes(data[:partial_len])

        sha256 = hashlib.sha256(data).hexdigest()
        installer.download_file_with_resume(
            url=url,
            target_path=target,
            expected_size=len(data),
            expected_sha256=sha256,
            max_retries=2,
            timeout=5.0,
            show_progress=False,
        )

        assert target.exists()
        assert target.read_bytes() == data
        assert not part.exists()
        assert server.last_headers.get("Range") == f"bytes={partial_len}-"
    finally:
        server.shutdown()


def test_download_server_ignores_range_200(installer, tmp_path):
    data = b"XYZ123456789" * 400
    server = MockRangeServer(data)
    server.ignore_range = True  # Server liefert immer 200 OK
    try:
        url = f"http://127.0.0.1:{server.port}/ignore_range.gguf"
        target = tmp_path / "ignore_range.gguf"
        part = target.with_name(target.name + ".part")

        # Vorhandene Part-Datei mit 500 Bytes
        part.write_bytes(data[:500])

        sha256 = hashlib.sha256(data).hexdigest()
        installer.download_file_with_resume(
            url=url,
            target_path=target,
            expected_size=len(data),
            expected_sha256=sha256,
            max_retries=2,
            timeout=5.0,
            show_progress=False,
        )

        # Vollstaendige Datei muss trotz 200 von vorn sauber geladen sein
        assert target.exists()
        assert target.read_bytes() == data
        assert not part.exists()
    finally:
        server.shutdown()


def test_download_corrupt_checksum_fails(installer, tmp_path):
    data = b"Originaldaten" * 100
    server = MockRangeServer(data)
    server.corrupt_data = True
    try:
        url = f"http://127.0.0.1:{server.port}/corrupt.gguf"
        target = tmp_path / "corrupt.gguf"
        part = target.with_name(target.name + ".part")

        expected_sha256 = hashlib.sha256(data).hexdigest()

        with pytest.raises(ValueError, match="Pruefsummenfehler"):
            installer.download_file_with_resume(
                url=url,
                target_path=target,
                expected_size=len(data),
                expected_sha256=expected_sha256,
                max_retries=1,
                timeout=5.0,
                show_progress=False,
            )

        # Ziel darf nicht angelegt sein und .part muss geloescht sein
        assert not target.exists()
        assert not part.exists()
    finally:
        server.shutdown()


# =============================================================================
# 3. Vorhandene Dateien und Pruefsummen-Verifikation
# =============================================================================

def test_verify_file_sha256_valid_and_corrupt(installer, tmp_path):
    content = b"Gepruefter Dateiinhalt fuer Testzwecke"
    f = tmp_path / "sample.bin"
    f.write_bytes(content)
    correct_sha = hashlib.sha256(content).hexdigest()
    wrong_sha = "0" * 64

    assert installer.verify_file_sha256(f, correct_sha, len(content), show_progress=False) is True
    assert installer.verify_file_sha256(f, wrong_sha, len(content), show_progress=False) is False


def test_existing_file_handling_in_execute_models_step(installer, tmp_path, capsys):
    content = b"Modelldaten" * 100
    sha = hashlib.sha256(content).hexdigest()

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    target_file = models_dir / "test_model.gguf"
    target_file.write_bytes(content)

    spec_file = tmp_path / "models.json"
    spec_file.write_text(json.dumps({
        "models": [{
            "id": "test",
            "title": "Test",
            "repo": "repo/test",
            "revision": "0" * 40,
            "files": [{
                "path": "test_model.gguf",
                "target": "test_model.gguf",
                "size": len(content),
                "sha256": sha
            }]
        }]
    }), encoding="utf-8")

    # Ausfuehren: Datei ist vorhanden und gueltig -> kein Download
    installer._execute_models_step(spec_file, str(models_dir), non_interactive=True)
    out = capsys.readouterr().out
    assert "vorhanden, geprueft" in out

    # Test mit falscher Groesse
    target_file.write_bytes(content[:50])
    installer._execute_models_step(spec_file, str(models_dir), non_interactive=True)
    out2 = capsys.readouterr().out
    assert "Dateigroesse weicht ab" in out2


# =============================================================================
# 4. Server-Adresse und daraus erzeugte .env-Werte
# =============================================================================

def test_server_url_validation_valid(installer):
    valid_urls = [
        "https://argus.example.org",
        "https://argus.example.org:8443",
        "https://argus.my-domain.de",
        "https://sub.domain.corp:9000",
        "https://argus.example.org/",
    ]
    for u in valid_urls:
        valid, err = installer.validate_server_url(u)
        assert valid, f"{u} sollte gueltig sein, Fehler: {err}"


def test_server_url_validation_invalid(installer):
    invalid_urls = [
        ("http://argus.example.org", "muss mit https:// beginnen"),
        ("https://argus.example.org/path", "darf keinen Pfad enthalten"),
        ("https://argus.example.org/api/v1", "darf keinen Pfad enthalten"),
        ("", "darf nicht leer sein"),
        ("https://", "einen Hostnamen enthalten"),
        ("https://argus.example.org?query=1", "keine Abfrageparameter"),
        ("https://argus.example.org#section", "kein Fragment"),
        ("ftp://argus.example.org", "muss mit https:// beginnen"),
    ]
    for u, expected_msg in invalid_urls:
        valid, err = installer.validate_server_url(u)
        assert not valid, f"{u} sollte ungueltig sein"
        assert expected_msg in err, f"Erwartete '{expected_msg}' in '{err}'"


def test_compute_server_env_values(installer):
    # Ohne Port
    env1 = installer.compute_server_env("https://argus.example.org")
    assert env1["DASHBOARD_ORIGINS"] == "https://argus.example.org,http://127.0.0.1:7860,http://localhost:7860"
    assert env1["TRUSTED_HOSTS"] == "argus.example.org,127.0.0.1,localhost,rag-backend"
    assert env1["DASHBOARD_TRACES_URL"] == ""

    # Mit Port
    env2 = installer.compute_server_env("https://argus.example.org:8443")
    assert env2["DASHBOARD_ORIGINS"] == "https://argus.example.org:8443,http://127.0.0.1:7860,http://localhost:7860"
    assert env2["TRUSTED_HOSTS"] == "argus.example.org,127.0.0.1,localhost,rag-backend"
    assert env2["DASHBOARD_TRACES_URL"] == ""

    # Mit nachgestelltem Slash (wird bereinigt)
    env3 = installer.compute_server_env("https://argus.example.org/")
    assert env3["DASHBOARD_ORIGINS"] == "https://argus.example.org,http://127.0.0.1:7860,http://localhost:7860"
    assert env3["TRUSTED_HOSTS"] == "argus.example.org,127.0.0.1,localhost,rag-backend"


# =============================================================================
# 5. Freier Speicherplatz
# =============================================================================

def test_check_disk_space_logic(installer, tmp_path, monkeypatch):
    Usage = collections.namedtuple("usage", ["total", "used", "free"])

    # Fall 1: Zu wenig Platz
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, 95 * 1024**3, 5 * 1024**3))
    has_space, free, req = installer.check_disk_space(
        tmp_path, total_needed_bytes=10 * 1024**3, reserve_bytes=1024**3
    )
    assert has_space is False
    assert req == 11 * 1024**3
    assert free == 5 * 1024**3

    # Fall 2: Genuegend Platz
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3))
    has_space, free, req = installer.check_disk_space(
        tmp_path, total_needed_bytes=10 * 1024**3, reserve_bytes=1024**3
    )
    assert has_space is True
    assert free == 50 * 1024**3


def test_execute_models_step_insufficient_space_warning(installer, tmp_path, monkeypatch, capsys):
    Usage = collections.namedtuple("usage", ["total", "used", "free"])
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, 99 * 1024**3, 100 * 1024))

    spec_file = tmp_path / "models.json"
    spec_file.write_text(json.dumps({
        "models": [{
            "id": "large_model",
            "title": "Large Model",
            "repo": "org/repo",
            "revision": "0" * 40,
            "files": [{
                "path": "model.gguf",
                "target": "model.gguf",
                "size": 5 * 1024 * 1024 * 1024,
                "sha256": "0" * 64
            }]
        }]
    }), encoding="utf-8")

    models_dir = tmp_path / "models"
    installer._execute_models_step(spec_file, str(models_dir), non_interactive=True)
    out = capsys.readouterr().out
    assert "Zu wenig freier Speicherplatz" in out


# =============================================================================
# 6. Keine echten Netz- oder Docker-Aufrufe in Unit-Tests (Monkeypatching)
# =============================================================================

def test_hardware_gpu_check_parsing(installer, monkeypatch):
    class FakeProcess:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode

    # Fall 1: nvidia-smi vorhanden
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: FakeProcess("NVIDIA GeForce RTX 5070 Ti, 16376 MiB\n")
    )
    has_gpu, name, vram = installer.check_gpu()
    assert has_gpu is True
    assert name == "NVIDIA GeForce RTX 5070 Ti"
    assert vram == 16376

    # Fall 2: nvidia-smi schlaegt fehl
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: FakeProcess("", returncode=1)
    )
    has_gpu, name, vram = installer.check_gpu()
    assert has_gpu is False
    assert vram == 0


def test_compose_status_parsing(installer, tmp_path, monkeypatch):
    class FakeProcess:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode

    # JSON Array
    array_out = json.dumps([
        {"Service": "rag-backend", "State": "running", "Health": "healthy"},
        {"Service": "llm", "State": "running", "Health": ""},
    ])
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: FakeProcess(array_out)
    )
    status = installer.get_compose_status(tmp_path)
    assert "rag-backend: running (healthy)" in status
    assert "llm: running" in status

    # Newline-getrenntes JSON
    lines_out = '{"Service": "postgres", "State": "running"}\n{"Service": "searxng", "State": "starting"}'
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: FakeProcess(lines_out)
    )
    status2 = installer.get_compose_status(tmp_path)
    assert "postgres: running" in status2
    assert "searxng: starting" in status2


# =============================================================================
# 7. Laufzeitordner, Doctor-Pruefungen und Unterbefehle
# =============================================================================

def test_ensure_installation_dirs_creates_workspace_uploads_evals_and_models(installer, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "init_stack_copy.py")
    (repo / "setup").mkdir()
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")

    ws = tmp_path / "ws"
    evals = tmp_path / "custom_evals"
    models = tmp_path / "models"
    (repo / ".env").write_text(f"ARGUS_EVALS_DIR={evals.as_posix()}\n", encoding="utf-8")

    created = installer.ensure_installation_dirs(repo, str(ws), str(models))
    assert ws.is_dir()
    assert (ws / "Uploads").is_dir()
    assert evals.is_dir()
    assert models.is_dir()
    assert len(created) == 4


def test_doctor_check_docker_and_gpu_and_ram(installer, monkeypatch):
    import os

    # 1. Docker fehlt
    monkeypatch.setattr(installer, "run_command", lambda cmd: (127, "", "not found"))
    r1 = installer.doctor_check_docker()
    assert r1.status == "FEHLER"

    # 2. Docker da, aber Daemon steht
    monkeypatch.setattr(
        installer,
        "run_command",
        lambda cmd: (0, "ok", "") if cmd[:2] in (["docker", "version"], ["docker", "compose"]) else (1, "", "down"),
    )
    r2 = installer.doctor_check_docker()
    assert r2.status == "FEHLER"

    # 3. Unter Linux: NVIDIA Container Toolkit fehlt -> FEHLER; mit nvidia -> OK
    if os.name != "nt":
        monkeypatch.setattr(installer, "run_command", lambda cmd: (0, "Runtimes: runc", ""))
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert installer.doctor_check_docker().status == "FEHLER"

        monkeypatch.setattr(installer, "run_command", lambda cmd: (0, "Runtimes: io.containerd.runc.v2 nvidia", ""))
        assert installer.doctor_check_docker().status == "OK"

    # 4. GPU & RAM
    monkeypatch.setattr(installer, "check_gpu", lambda: (False, "", 0))
    assert installer.doctor_check_gpu().status == "FEHLER"

    monkeypatch.setattr(installer, "check_gpu", lambda: (True, "RTX 4060", 8192))
    assert installer.doctor_check_gpu().status == "WARNUNG"

    monkeypatch.setattr(installer, "check_gpu", lambda: (True, "RTX 5070 Ti", 16376))
    assert installer.doctor_check_gpu().status == "OK"

    monkeypatch.setattr(installer, "check_ram_bytes", lambda: 16 * 1024**3)
    assert installer.doctor_check_ram().status == "WARNUNG"

    monkeypatch.setattr(installer, "check_ram_bytes", lambda: 64 * 1024**3)
    assert installer.doctor_check_ram().status == "OK"


def test_doctor_check_disk_space_is_read_only(installer, tmp_path, monkeypatch):
    Usage = collections.namedtuple("usage", ["total", "used", "free"])
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, 20 * 1024**3, 80 * 1024**3))

    non_existent_models = tmp_path / "noch_nicht_angelegt" / "models"
    res = installer.doctor_check_disk_space(non_existent_models)
    assert res.status == "OK"
    # doctor darf den fehlenden Ordner NICHT anlegen
    assert not non_existent_models.exists()


def test_doctor_check_env_and_switches(installer, tmp_path):
    import os

    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")

    # 1. .env fehlt
    assert installer.doctor_check_env(repo).status == "FEHLER"

    ws = tmp_path / "ws"
    evals = tmp_path / "ws" / "Evals"
    models = tmp_path / "models"
    ws.mkdir()
    evals.mkdir()
    models.mkdir()

    (repo / ".env.example").write_text(
        "ARGUS_MODELS_DIR=C:/Argus_Models\n"
        "ARGUS_WORKSPACE_DIR=C:/Argus_Workspace\n"
        "ARGUS_EVALS_DIR=C:/Argus_Workspace/Evals\n"
        "SEARXNG_LANGUAGE=de-DE\n"
        "EXTRA_KEY=1\n",
        encoding="utf-8",
    )

    # 2. Fehlender Schluessel EXTRA_KEY -> WARNUNG (nur Name, kein Wert)
    (repo / ".env").write_text(
        f"ARGUS_MODELS_DIR={models.as_posix()}\n"
        f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
        f"ARGUS_EVALS_DIR={evals.as_posix()}\n"
        "SEARXNG_LANGUAGE=de-DE\n",
        encoding="utf-8",
    )
    r_warn = installer.doctor_check_env(repo)
    assert r_warn.status == "WARNUNG"
    assert "EXTRA_KEY" in r_warn.message

    # 3. Alle Schluessel da -> OK
    (repo / ".env").write_text(
        (repo / ".env").read_text(encoding="utf-8") + "EXTRA_KEY=geheimer_wert_xyz\n",
        encoding="utf-8",
    )
    r_ok = installer.doctor_check_env(repo)
    assert r_ok.status == "OK"
    assert "geheimer_wert_xyz" not in r_ok.message

    # 4. Unter Linux: Windows-Pfad in ARGUS_*_DIR -> FEHLER
    if os.name != "nt":
        (repo / ".env").write_text(
            "ARGUS_MODELS_DIR=C:/Argus_Models\n"
            f"ARGUS_WORKSPACE_DIR={ws.as_posix()}\n"
            f"ARGUS_EVALS_DIR={evals.as_posix()}\n"
            "SEARXNG_LANGUAGE=de-DE\n"
            "EXTRA_KEY=1\n",
            encoding="utf-8",
        )
        r_win = installer.doctor_check_env(repo)
        assert r_win.status == "FEHLER"
        assert "ARGUS_MODELS_DIR" in r_win.message

    # 5. Sicherheitsschalter pruefen
    safe_env = {
        "CLOUD_AGENTS_ENABLED": "false",
        "CLOUD_MISSION_AUTO_APPROVE": "false",
        "DASHBOARD_SETUP_TOKEN": "geheimer-setup-token-wert",
        "EGRESS_AFTER_SENSITIVE": "block",
        "WEB_INJECTION_GUARD": "true",
        "API_DOCS": "false",
    }
    sw_ok = installer.doctor_check_switches(safe_env)
    assert len(sw_ok) == 1 and sw_ok[0].status == "OK"
    assert "geheimer-setup-token-wert" not in sw_ok[0].message

    risky_env = {
        "CLOUD_AGENTS_ENABLED": "true",
        "CLOUD_MISSION_AUTO_APPROVE": "true",
        "DASHBOARD_SETUP_TOKEN": "",
        "EGRESS_AFTER_SENSITIVE": "allow",
        "WEB_INJECTION_GUARD": "false",
        "API_DOCS": "true",
    }
    sw_warn = installer.doctor_check_switches(risky_env)
    assert len(sw_warn) == 6
    assert all(w.status == "WARNUNG" for w in sw_warn)
    combined = " ".join(w.message for w in sw_warn)
    for expected_name in (
        "CLOUD_AGENTS_ENABLED",
        "CLOUD_MISSION_AUTO_APPROVE",
        "DASHBOARD_SETUP_TOKEN",
        "EGRESS_AFTER_SENSITIVE",
        "WEB_INJECTION_GUARD",
        "API_DOCS",
    ):
        assert expected_name in combined


def test_doctor_check_secrets_and_dirs_permissions_and_no_secret_leak(installer, tmp_path):
    import os

    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(_SCRIPT.parent.parent / "docker-compose.yml", repo / "docker-compose.yml")
    shutil.copy2(_SCRIPT.parent.parent / ".env.example", repo / ".env.example")

    ws = tmp_path / "ws"
    models = tmp_path / "models"
    init_mod = installer._load_init_stack_module(repo)
    assert init_mod.main(["--stack", str(repo), "--workspace", str(ws), "--models", str(models), "--ohne-naechste-schritte"]) == 0

    res = installer.doctor_check_secrets_and_dirs(repo)
    assert res.status == "OK", res.message

    # Keine Geheimniswerte im Ergebnis
    for p in (repo / "API_Tokens").iterdir():
        val = p.read_text(encoding="utf-8").strip()
        if val:
            assert val not in res.message

    if os.name != "nt":
        # Falsche Dateirechte (0600 statt 0644) -> FEHLER
        os.chmod(repo / "API_Tokens" / "fernet_key.txt", 0o600)
        res_bad_file = installer.doctor_check_secrets_and_dirs(repo)
        assert res_bad_file.status == "FEHLER"
        assert "fernet_key.txt" in res_bad_file.message
        os.chmod(repo / "API_Tokens" / "fernet_key.txt", 0o644)

        # Falsche Ordnerrechte auf API_Tokens/ (0755 statt 0700) -> FEHLER
        os.chmod(repo / "API_Tokens", 0o755)
        res_bad_dir = installer.doctor_check_secrets_and_dirs(repo)
        assert res_bad_dir.status == "FEHLER"
        assert "API_Tokens/" in res_bad_dir.message
        os.chmod(repo / "API_Tokens", 0o700)

        # Fremde ARGUS_UID ohne Schreibrecht auf Arbeitsordner (0755) -> FEHLER
        os.chmod(ws, 0o755)
        env_text = (repo / ".env").read_text(encoding="utf-8")
        (repo / ".env").write_text(
            init_mod.set_env_values(env_text, {"ARGUS_UID": "4242", "ARGUS_GID": "4242"}),
            encoding="utf-8",
        )
        res_unwritable = installer.doctor_check_secrets_and_dirs(repo)
        assert res_unwritable.status == "FEHLER"
        assert "4242" in res_unwritable.message


def test_doctor_check_models_size_and_verify_sha256(installer, tmp_path):
    data = b"Testmodell-Inhalt" * 50
    sha = hashlib.sha256(data).hexdigest()
    spec_path = tmp_path / "models.json"
    spec_path.write_text(json.dumps({
        "models": [{
            "id": "m1",
            "title": "Test",
            "repo": "org/m1",
            "revision": "a" * 40,
            "files": [{"path": "m1.gguf", "target": "m1.gguf", "size": len(data), "sha256": sha}],
        }]
    }), encoding="utf-8")

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    target = models_dir / "m1.gguf"

    # 1. Datei fehlt
    assert installer.doctor_check_models(models_dir, spec_path, verify_sha256=False).status == "FEHLER"

    # 2. Richtige Groesse, aber korrupter Inhalt: ohne --verify-models OK, mit --verify-models FEHLER
    target.write_bytes(b"X" * len(data))
    assert installer.doctor_check_models(models_dir, spec_path, verify_sha256=False).status == "OK"
    assert installer.doctor_check_models(models_dir, spec_path, verify_sha256=True).status == "FEHLER"

    # 3. Richtiger Inhalt -> auch mit --verify-models OK
    target.write_bytes(data)
    assert installer.doctor_check_models(models_dir, spec_path, verify_sha256=True).status == "OK"


def test_doctor_check_selinux_and_stack(installer, tmp_path, monkeypatch):
    import os

    if os.name != "nt":
        enforce_f = tmp_path / "enforce"
        enforce_f.write_text("1\n", encoding="utf-8")
        assert installer.doctor_check_selinux(enforce_f).status == "WARNUNG"

        enforce_f.write_text("0\n", encoding="utf-8")
        monkeypatch.setattr(shutil, "which", lambda _: None)
        assert installer.doctor_check_selinux(enforce_f).status == "OK"

    # Stack-Pruefung: laeuft und /healthz antwortet -> OK
    class FakeProc:
        returncode = 0
        stdout = '[{"Service": "rag-backend", "State": "running", "Health": "healthy"}]'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(installer, "check_healthz", lambda url="": True)
    assert installer.doctor_check_stack(tmp_path).status == "OK"

    # Container exited -> FEHLER
    class FakeProcExited:
        returncode = 0
        stdout = '[{"Service": "rag-backend", "State": "exited", "Health": ""}]'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProcExited())
    assert installer.doctor_check_stack(tmp_path).status == "FEHLER"


def test_subcommand_reset_password_passes_via_stdin(installer, monkeypatch, capsys):
    import getpass

    recorded: list[tuple[list[str], str]] = []

    class FakeExecResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        recorded.append((list(cmd), kwargs.get("input", "")))
        return FakeExecResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # 1. Nicht uebereinstimmende Passwoerter -> Fehler 1, kein docker compose exec
    inputs = iter(["PasswortEins123!", "PasswortZwei123!"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(inputs))
    assert installer.main(["reset-password", "admin"]) == 1
    assert recorded == []
    assert "stimmen nicht ueberein" in capsys.readouterr().err

    # 2. Zu kurzes Passwort -> Fehler 1, kein docker compose exec
    inputs_short = iter(["kurz", "kurz"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(inputs_short))
    assert installer.main(["reset-password", "admin"]) == 1
    assert recorded == []
    assert "8 Zeichen" in capsys.readouterr().err

    # 3. Gueltiges Passwort -> Aufruf von docker compose exec -T rag-backend python -m rag_backend.admin_cli reset-password admin
    inputs_ok = iter(["SicheresPasswort99!", "SicheresPasswort99!"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(inputs_ok))
    assert installer.main(["reset-password", "admin"]) == 0
    assert len(recorded) == 1
    cmd, stdin_data = recorded[0]
    assert cmd == [
        "docker", "compose", "exec", "-T", "rag-backend",
        "python", "-m", "rag_backend.admin_cli", "reset-password", "admin",
    ]
    assert stdin_data == "SicheresPasswort99!\n"
    assert "SicheresPasswort99!" not in str(cmd)


def test_subcommand_doctor_cli_exit_code(installer, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        installer,
        "run_doctor",
        lambda root, verify_models=False: (
            0,
            [installer.DoctorResult("OK", "Test", f"verify={verify_models}")],
        ),
    )
    rc = installer.main(["doctor", "--verify-models", "--stack", str(tmp_path)])
    assert rc == 0



def test_doctor_uses_compose_defaults_for_missing_dirs_only_on_windows(installer, tmp_path):
    #--- Aeltere .env ohne ARGUS_MODELS_DIR: Compose nimmt dann seinen Standardwert.
    #--- doctor darf das unter Windows nicht als FEHLER melden (der Stapel laeuft so),
    #--- unter Linux taugt der Windows-Standard nicht.
    root = _SCRIPT.parent.parent
    defaults = installer._compose_dir_defaults(root)
    assert defaults["ARGUS_MODELS_DIR"] == "C:/Argus_Models"
    assert defaults["ARGUS_WORKSPACE_DIR"] == "C:/Argus_Workspace"
    assert defaults["ARGUS_EVALS_DIR"] == "C:/Argus_Workspace/Evals"

    env = {"ARGUS_EVALS_DIR": "/srv/argus/evals"}
    assert installer._effective_dir(root, env, "ARGUS_EVALS_DIR", windows=True) == ("/srv/argus/evals", False)
    assert installer._effective_dir(root, env, "ARGUS_MODELS_DIR", windows=True) == ("C:/Argus_Models", True)
    assert installer._effective_dir(root, env, "ARGUS_MODELS_DIR", windows=False) == ("", False)
    # Ohne Compose-Datei gibt es keinen Standard.
    assert installer._effective_dir(tmp_path, env, "ARGUS_MODELS_DIR", windows=True) == ("", False)


# =============================================================================
# 8. TLS-Proxy-Optionen, .env-Werte und Doctor-Pruefungen (Auftrag C)
# =============================================================================


def test_compute_and_update_server_env_with_proxy(installer, tmp_path):
    # 1. Mit Proxy (Standard internal) und leerer Vorbelegung
    env_p = installer.compute_server_env(
        "https://argus.example.org:8443",
        use_proxy=True,
        tls_mode="internal",
        existing_env={},
    )
    assert env_p["DASHBOARD_ORIGINS"] == "https://argus.example.org:8443,http://127.0.0.1:7860,http://localhost:7860"
    assert env_p["TRUSTED_HOSTS"] == "argus.example.org,127.0.0.1,localhost,rag-backend"
    assert env_p["COMPOSE_PROFILES"] == "proxy"
    assert env_p["ARGUS_DOMAIN"] == "argus.example.org"
    assert env_p["ARGUS_TLS_MODE"] == "internal"
    assert env_p["ARGUS_TRUSTED_PROXIES"] == "172.31.250.0/29"

    # 2. Vorhandene COMPOSE_PROFILES und eigenes ARGUS_EDGE_SUBNET bleiben erhalten
    env_keep = installer.compute_server_env(
        "https://ki.firma.de",
        use_proxy=True,
        tls_mode="files",
        existing_env={"COMPOSE_PROFILES": "monitoring,extra", "ARGUS_EDGE_SUBNET": "10.250.0.0/29"},
    )
    assert env_keep["COMPOSE_PROFILES"] == "monitoring,extra,proxy"
    assert env_keep["ARGUS_DOMAIN"] == "ki.firma.de"
    assert env_keep["ARGUS_TLS_MODE"] == "files"
    assert env_keep["ARGUS_TRUSTED_PROXIES"] == "10.250.0.0/29"

    # 3. Ungueltiger TLS-Modus wirft ValueError
    with pytest.raises(ValueError, match="TLS-Modus"):
        installer.compute_server_env("https://ki.firma.de", use_proxy=True, tls_mode="ungueltig")

    # 4. update_server_env schreibt Werte in .env
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(_SCRIPT.parent.parent / ".env.example", repo / ".env.example")
    init_mod = installer._load_init_stack_module(repo)
    (repo / ".env").write_text("COMPOSE_PROFILES=alt\n", encoding="utf-8")

    installer.update_server_env(repo, "https://argus.firma.de", use_proxy=True, tls_mode="acme")
    parsed = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert parsed["COMPOSE_PROFILES"] == "alt,proxy"
    assert parsed["ARGUS_DOMAIN"] == "argus.firma.de"
    assert parsed["ARGUS_TLS_MODE"] == "acme"
    assert parsed["ARGUS_TRUSTED_PROXIES"] == "172.31.250.0/29"


def test_installer_main_proxy_cli_and_interactive_questions(installer, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(_SCRIPT.parent.parent / ".env.example", repo / ".env.example")
    shutil.copy2(_SCRIPT.parent.parent / "docker-compose.yml", repo / "docker-compose.yml")
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
    monkeypatch.setattr(installer.Path, "resolve", lambda self, *a, **kw: repo / "setup" / "install.py" if self == Path(installer.__file__) else os.path.abspath(self))
    monkeypatch.setattr(installer, "__file__", str(repo / "setup" / "install.py"))
    monkeypatch.setattr(installer.webbrowser, "open", lambda url: True)

    # 1. --server-url mit --yes: Proxy ist standardmaessig aktiv (internal)
    rc1 = installer.main([
        "--yes", "--no-models", "--no-start",
        "--workspace", str(ws), "--models", str(models),
        "--server-url", "https://argus.example.org",
    ])
    assert rc1 == 0
    env1 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env1["COMPOSE_PROFILES"] == "proxy"
    assert env1["ARGUS_DOMAIN"] == "argus.example.org"
    assert env1["ARGUS_TLS_MODE"] == "internal"
    assert env1["ARGUS_TRUSTED_PROXIES"] == "172.31.250.0/29"

    # 2. --server-url mit --no-proxy: keine Proxy-Werte gesetzt
    (repo / ".env").unlink()
    rc2 = installer.main([
        "--yes", "--no-models", "--no-start", "--no-proxy",
        "--workspace", str(ws), "--models", str(models),
        "--server-url", "https://argus.example.org",
    ])
    assert rc2 == 0
    env2 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env2.get("COMPOSE_PROFILES", "") == ""
    assert env2.get("ARGUS_TRUSTED_PROXIES", "") == ""

    # 3. Interaktive Fragen (Auswahl 2 -> URL -> Proxy Ja -> TLS-Modus acme)
    (repo / ".env").unlink()
    answers = iter(["2", "https://interaktiv.example.org", "j", "acme", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc3 = installer.main([
        "--no-models", "--no-start",
        "--workspace", str(ws), "--models", str(models),
    ])
    assert rc3 == 0
    env3 = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env3["COMPOSE_PROFILES"] == "proxy"
    assert env3["ARGUS_DOMAIN"] == "interaktiv.example.org"
    assert env3["ARGUS_TLS_MODE"] == "acme"
    assert env3["ARGUS_TRUSTED_PROXIES"] == "172.31.250.0/29"


def _own_ugid() -> dict[str, str]:
    """ARGUS_UID/GID = der Nutzer, der die Tests ausfuehrt (wie init_stack es unter Linux
    eintraegt). Ohne sie prueft doctor gegen 1000:1000, den Standard aus compose."""
    if not hasattr(os, "getuid"):
        return {}
    return {"ARGUS_UID": str(os.getuid()), "ARGUS_GID": str(os.getgid())}


def test_doctor_check_proxy_states(installer, tmp_path):
    repo = tmp_path / "repo"
    (repo / "proxy" / "certs").mkdir(parents=True)
    (repo / "proxy" / "data").mkdir(parents=True)

    # 1. Weder Profil proxy noch ARGUS_TRUSTED_PROXIES -> OK
    r_local = installer.doctor_check_proxy(repo, {})
    assert r_local.status == "OK"

    # 2. ARGUS_TRUSTED_PROXIES ohne Profil proxy gesetzt -> WARNUNG
    r_warn = installer.doctor_check_proxy(repo, {"ARGUS_TRUSTED_PROXIES": "10.0.0.1"})
    assert r_warn.status == "WARNUNG"
    assert "ARGUS_TRUSTED_PROXIES" in r_warn.message

    # 3. Profil proxy aktiv, alles gueltig (internal) -> OK
    valid_proxy_env = {
        "COMPOSE_PROFILES": "proxy",
        "ARGUS_DOMAIN": "argus.example.org",
        "ARGUS_TLS_MODE": "internal",
        "ARGUS_TRUSTED_PROXIES": "172.31.250.0/29",
        "TRUSTED_HOSTS": "argus.example.org,127.0.0.1,localhost,rag-backend",
        "DASHBOARD_ORIGINS": "https://argus.example.org,http://127.0.0.1:7860",
        **_own_ugid(),
    }
    assert installer.doctor_check_proxy(repo, valid_proxy_env).status == "OK"

    # 3a. Domain fehlt in TRUSTED_HOSTS bzw. DASHBOARD_ORIGINS -> FEHLER
    r_host = installer.doctor_check_proxy(repo, dict(valid_proxy_env, TRUSTED_HOSTS="127.0.0.1,localhost"))
    assert r_host.status == "FEHLER" and "TRUSTED_HOSTS" in r_host.message
    r_origin = installer.doctor_check_proxy(
        repo, dict(valid_proxy_env, DASHBOARD_ORIGINS="http://argus.example.org,http://127.0.0.1:7860")
    )
    assert r_origin.status == "FEHLER" and "DASHBOARD_ORIGINS" in r_origin.message
    # Mit Port in der Adresse ist der Eintrag gueltig
    assert installer.doctor_check_proxy(
        repo, dict(valid_proxy_env, DASHBOARD_ORIGINS="https://argus.example.org:8443")
    ).status == "OK"

    # 3b. Datenordner fehlt -> FEHLER (sonst legte Docker ihn unter Linux root-eigen an)
    r_nodata = installer.doctor_check_proxy(repo, dict(valid_proxy_env, ARGUS_PROXY_DATA_DIR="proxy/fehlt"))
    assert r_nodata.status == "FEHLER" and "Datenordner" in r_nodata.message

    # 4. Profil proxy aktiv, ARGUS_DOMAIN fehlt -> FEHLER
    no_domain = dict(valid_proxy_env, ARGUS_DOMAIN="")
    r_nodom = installer.doctor_check_proxy(repo, no_domain)
    assert r_nodom.status == "FEHLER" and "ARGUS_DOMAIN" in r_nodom.message

    # 5. Profil proxy aktiv, ARGUS_TLS_MODE ungueltig -> FEHLER
    bad_tls = dict(valid_proxy_env, ARGUS_TLS_MODE="selfsigned")
    r_badtls = installer.doctor_check_proxy(repo, bad_tls)
    assert r_badtls.status == "FEHLER" and "ARGUS_TLS_MODE" in r_badtls.message

    # 6. Profil proxy aktiv, ARGUS_TRUSTED_PROXIES fehlt -> FEHLER
    no_trusted = dict(valid_proxy_env, ARGUS_TRUSTED_PROXIES="")
    r_notrusted = installer.doctor_check_proxy(repo, no_trusted)
    assert r_notrusted.status == "FEHLER" and "ARGUS_TRUSTED_PROXIES" in r_notrusted.message

    # 7. Profil proxy aktiv mit ARGUS_TLS_MODE=files: ohne Dateien FEHLER, mit Dateien OK
    files_env = dict(valid_proxy_env, ARGUS_TLS_MODE="files")
    r_missing_files = installer.doctor_check_proxy(repo, files_env)
    assert r_missing_files.status == "FEHLER"
    assert "cert.pem" in r_missing_files.message and "key.pem" in r_missing_files.message

    (repo / "proxy" / "certs" / "cert.pem").write_text("CERT", encoding="utf-8")
    (repo / "proxy" / "certs" / "key.pem").write_text("KEY", encoding="utf-8")
    assert installer.doctor_check_proxy(repo, files_env).status == "OK"

    # 8. Unter Linux: Schluessel 0600 eines anderen Nutzers ist fuer ARGUS_UID nicht lesbar
    if os.name != "nt":
        key = repo / "proxy" / "certs" / "key.pem"
        key.chmod(0o600)
        own_uid = os.getuid()
        ok_env = dict(files_env, ARGUS_UID=str(own_uid), ARGUS_GID=str(os.getgid()))
        assert installer.doctor_check_proxy(repo, ok_env).status == "OK"
        other_env = dict(files_env, ARGUS_UID=str(own_uid + 4242), ARGUS_GID=str(own_uid + 4242))
        r_unreadable = installer.doctor_check_proxy(repo, other_env)
        assert r_unreadable.status == "FEHLER" and "key.pem" in r_unreadable.message


def test_update_server_env_with_proxy_creates_dirs_and_port(installer, tmp_path):
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(_SCRIPT.parent / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(_SCRIPT.parent.parent / ".env.example", repo / ".env.example")
    init_mod = installer._load_init_stack_module(repo)

    installer.update_server_env(repo, "https://ki.firma.de:8443", use_proxy=True, tls_mode="files")
    parsed = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert parsed["ARGUS_PROXY_HTTPS_PORT"] == "8443"
    assert (repo / "proxy" / "data").is_dir() and (repo / "proxy" / "certs").is_dir()
    #--- Was der Installer schreibt, besteht die eigene Pruefung (bis auf die Zertifikate).
    #--- ARGUS_UID setzt sonst init_stack; der Installer laeuft hier ohne.
    r = installer.doctor_check_proxy(repo, {**parsed, **_own_ugid()})
    assert r.status == "FEHLER" and "cert.pem" in r.message
    assert "TRUSTED_HOSTS" not in r.message and "DASHBOARD_ORIGINS" not in r.message
    assert "Datenordner" not in r.message
