"""Tests fuer die gefuehrte Installation (setup/install.py)."""
from __future__ import annotations

import collections
import hashlib
import http.server
import importlib.util
import json
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
