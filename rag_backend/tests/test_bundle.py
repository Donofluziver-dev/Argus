"""Offline-Paket (install bundle / --bundle) und fertige Images aus einer Registry."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def installer():
    spec = importlib.util.spec_from_file_location("install_bundle_test", REPO_ROOT / "setup" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "setup").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "setup" / "init_stack.py", repo / "setup" / "init_stack.py")
    shutil.copy2(REPO_ROOT / ".env.example", repo / ".env.example")
    return repo


class FakeDocker:
    """Ersetzt subprocess.run: merkt sich die Befehle, liefert eine compose-Konfiguration
    und legt bei 'docker save' die Datei an."""

    def __init__(self, present: set[str] | None = None, load_ok: bool = True):
        self.calls: list[list[str]] = []
        self.present = set(present or ())
        self.load_ok = load_ok
        self.config = {"services": {
            "rag-backend": {"build": {"context": "."}, "image": "argus/rag-backend:v1"},
            "caddy": {"image": "caddy:2.11-alpine@sha256:" + "a" * 64},
        }}

    def __call__(self, cmd, *a, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if cmd[:2] == ["docker", "compose"] and "config" in cmd:
            r.stdout = json.dumps(self.config)
        elif cmd[:3] == ["docker", "image", "inspect"]:
            r.returncode = 0 if cmd[3] in self.present else 1
        elif cmd[:2] == ["docker", "save"]:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"IMAGES")
        elif cmd[:2] == ["docker", "tag"]:
            self.present.add(cmd[3])
        elif cmd[:2] == ["docker", "load"] and not self.load_ok:
            r.returncode = 1
        return r


def test_create_bundle_builds_pulls_tags_and_writes_manifest(installer, tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(installer.subprocess, "run", fake)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    out = tmp_path / "paket"

    manifest = installer.create_bundle(tmp_path, out, "1.2.3")

    cmds = [" ".join(c) for c in fake.calls]
    #--- Eigene Images werden gebaut, mit Versionsnamen und ohne Pull.
    build = next(c for c in fake.calls if c[:2] == ["docker", "compose"] and "build" in c)
    assert build[-1] == "rag-backend"
    assert "--profile" in build and "voice" in build and "web" in build
    #--- Das fremde Image wird per Digest geholt und unter dem Paketnamen markiert.
    caddy_ref = fake.config["services"]["caddy"]["image"]
    assert f"docker pull {caddy_ref}" in cmds
    assert f"docker tag {caddy_ref} argus/caddy:1.2.3" in cmds
    save = next(c for c in fake.calls if c[:2] == ["docker", "save"])
    assert set(save[4:]) == {"argus/caddy:1.2.3", "argus/rag-backend:1.2.3"}

    override = (out / "docker-compose.bundle.yml").read_text(encoding="utf-8")
    assert "  caddy:\n    image: argus/caddy:1.2.3\n    pull_policy: never" in override
    assert "  rag-backend:\n    image: argus/rag-backend:1.2.3\n    pull_policy: never" in override
    #--- Wheels fuer die Hilfsumgebung der Installation, Linux und Windows.
    pip_calls = [c for c in fake.calls if "download" in c]
    assert any("manylinux2014_x86_64" in c for c in pip_calls)
    assert any("win_amd64" in c for c in pip_calls)
    assert all(c[-1] == installer.SETUP_PIP_REQUIREMENT for c in pip_calls)
    on_disk = json.loads((out / "argus-bundle.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert manifest["version"] == "1.2.3" and manifest["format"] == 1
    assert manifest["images_sha256"] == hashlib.sha256(b"IMAGES").hexdigest()


def test_create_bundle_skips_pull_for_present_images_and_checks_input(installer, tmp_path, monkeypatch):
    fake = FakeDocker(present={"caddy:2.11-alpine@sha256:" + "a" * 64})
    monkeypatch.setattr(installer.subprocess, "run", fake)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    installer.create_bundle(tmp_path, tmp_path / "p", "1.0", services=["caddy"])
    assert not any(c[:2] == ["docker", "pull"] for c in fake.calls)
    assert not any("build" in c for c in fake.calls if c[:2] == ["docker", "compose"])

    with pytest.raises(ValueError, match="Version"):
        installer.create_bundle(tmp_path, tmp_path / "x", "1.0; rm -rf /")
    with pytest.raises(ValueError, match="Unbekannte Dienste"):
        installer.create_bundle(tmp_path, tmp_path / "y", "1.0", services=["gibtsnicht"])


def _bundle_dir(tmp_path: Path, version: str = "2026.11.18-abc") -> Path:
    b = tmp_path / "bundle"
    (b / "models").mkdir(parents=True)
    (b / "images.tar").write_bytes(b"IMAGES")
    (b / "docker-compose.bundle.yml").write_text("services: {}\n", encoding="utf-8")
    manifest = {
        "format": 1, "version": version, "created": "x", "git_commit": "",
        "images": {"caddy": f"argus/caddy:{version}"},
        "images_sha256": hashlib.sha256(b"IMAGES").hexdigest(), "models": [],
    }
    (b / "argus-bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    return b


def test_apply_bundle_loads_images_and_switches_env(installer, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    bundle = _bundle_dir(tmp_path)
    fake = FakeDocker(present={"argus/caddy:2026.11.18-abc"})
    monkeypatch.setattr(installer.subprocess, "run", fake)

    installer.apply_bundle(repo, bundle)

    assert ["docker", "load", "-i", str(bundle / "images.tar")] in fake.calls
    assert (repo / "docker-compose.bundle.yml").is_file()
    init_mod = installer._load_init_stack_module(repo)
    env = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.bundle.yml"
    assert env["COMPOSE_PATH_SEPARATOR"] == ":"
    assert env["ARGUS_VERSION"] == "2026.11.18-abc"
    assert env["ARGUS_PULL_POLICY"] == "never"


def test_apply_bundle_rejects_damaged_or_incomplete_bundles(installer, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    bundle = _bundle_dir(tmp_path)
    monkeypatch.setattr(installer.subprocess, "run", FakeDocker())

    #--- Nach dem Laden fehlt ein Image -> Fehler, .env bleibt unberuehrt.
    with pytest.raises(RuntimeError, match="fehlen Images"):
        installer.apply_bundle(repo, bundle)
    assert not (repo / ".env").exists()

    #--- Beschaedigte images.tar -> Pruefsumme stimmt nicht, docker load laeuft gar nicht.
    fake = FakeDocker(present={"argus/caddy:2026.11.18-abc"})
    monkeypatch.setattr(installer.subprocess, "run", fake)
    (bundle / "images.tar").write_bytes(b"MANIPULIERT")
    with pytest.raises(ValueError, match="Pruefsumme"):
        installer.apply_bundle(repo, bundle)
    assert not any(c[:2] == ["docker", "load"] for c in fake.calls)

    #--- Kein Paket.
    with pytest.raises(ValueError, match="kein Argus-Offline-Paket"):
        installer.load_bundle_manifest(tmp_path / "leer")


def test_copy_models_from_bundle_only_missing(installer, tmp_path):
    spec = {"models": [{"repo": "r", "revision": "v", "files": [
        {"path": "a", "target": "a.gguf", "size": 3, "sha256": "x"},
        {"path": "b", "target": "b.gguf", "size": 3, "sha256": "y"},
    ]}]}
    spec_path = tmp_path / "models.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    bundle = tmp_path / "bundle"
    (bundle / "models").mkdir(parents=True)
    (bundle / "models" / "a.gguf").write_bytes(b"AAA")
    (bundle / "models" / "b.gguf").write_bytes(b"BBB")
    models = tmp_path / "models"
    models.mkdir()
    (models / "a.gguf").write_bytes(b"OLD")

    copied = installer.copy_models_from_bundle(bundle, models, spec_path)
    assert copied == 1
    assert (models / "a.gguf").read_bytes() == b"OLD"
    assert (models / "b.gguf").read_bytes() == b"BBB"


def test_setup_venv_installs_from_bundle_wheels_without_index(installer, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    py = repo / ".venv-setup" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    wheels = tmp_path / "bundle" / "wheels"
    wheels.mkdir(parents=True)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))

        class R:
            returncode = 1 if cmd[1:3] == ["-c", "import cryptography"] else 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.ensure_setup_venv(repo, wheels_dir=wheels)
    pip = next(c for c in calls if "install" in c)
    assert "--no-index" in pip and pip[pip.index("--find-links") + 1] == str(wheels)

    calls.clear()
    installer.ensure_setup_venv(repo)
    pip = next(c for c in calls if "install" in c)
    assert "--no-index" not in pip


def test_configure_registry_images(installer, tmp_path):
    repo = _repo(tmp_path)
    installer.configure_registry_images(repo, "ghcr.io/firma/argus", "1.0.0")
    init_mod = installer._load_init_stack_module(repo)
    env = init_mod.parse_env((repo / ".env").read_text(encoding="utf-8"))
    assert env["ARGUS_IMAGE_PREFIX"] == "ghcr.io/firma/argus"
    assert env["ARGUS_VERSION"] == "1.0.0"
    assert env["ARGUS_PULL_POLICY"] == "missing"
    for bad in ("GHCR.io/Firma", "ghcr.io/firma argus", "", "ghcr.io/firma/argus;x"):
        with pytest.raises(ValueError):
            installer.configure_registry_images(repo, bad, "1.0.0")


def test_installer_rejects_conflicting_image_options(installer, tmp_path, capsys):
    assert installer.main(["--image-prefix", "ghcr.io/firma/argus"]) == 1
    assert "--image-version" in capsys.readouterr().out
    bundle = _bundle_dir(tmp_path)
    assert installer.main(["--bundle", str(bundle), "--image-prefix", "ghcr.io/f/a",
                           "--image-version", "1"]) == 1
    assert installer.main(["--bundle", str(tmp_path / "gibtsnicht")]) == 1


def test_compose_built_services_never_pull_by_default():
    """Jeder selbst gebaute Dienst hat einen festen Namen und pull_policy never: sonst
    zoege Compose bei fehlendem Image ein fremdes gleichen Namens aus einer Registry."""
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    lines = text.splitlines()
    built = []
    for i, line in enumerate(lines):
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            block = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("  ") and not nxt.startswith("   ") and nxt.strip():
                    break
                block.append(nxt)
            if any(b.strip() == "build:" for b in block):
                name = line.strip().rstrip(":")
                built.append(name)
                assert f"    image: ${{ARGUS_IMAGE_PREFIX:-argus}}/{name}:${{ARGUS_VERSION:-local}}" in block, name
                assert "    pull_policy: ${ARGUS_PULL_POLICY:-never}" in block, name
    assert {"rag-backend", "calc-sandbox", "llm", "tts-service"} <= set(built)
