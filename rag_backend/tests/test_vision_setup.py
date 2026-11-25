"""Bildeingabe: Zusatzdatei des Modells (mmproj) in Installation, Startskript und Compose."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MMPROJ = "mmproj-Qwen3.6-35B-A3B-F16.gguf"


def _load_init_stack(monkeypatch):
    spec = importlib.util.spec_from_file_location("init_stack", REPO_ROOT / "setup" / "init_stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "restrict_to_owner", lambda _d: False)
    monkeypatch.setattr(mod.ensure_stack_secrets, "__defaults__", (lambda _d: False,))
    return mod


def test_mmproj_is_part_of_the_model_download():
    spec = json.loads((REPO_ROOT / "setup" / "models.json").read_text(encoding="utf-8"))
    llm = next(m for m in spec["models"] if m["id"] == "llm")
    entry = next(f for f in llm["files"] if f["target"] == MMPROJ)
    assert entry["size"] > 100_000_000 and len(entry["sha256"]) == 64
    #--- Dieselbe Revision wie das Modell: Encoder und Modell muessen zusammenpassen.
    assert entry["path"].startswith("mmproj-")


def test_template_names_the_mmproj_of_the_default_model():
    from_template = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            from_template[k.strip()] = v.strip()
    assert from_template["LLM_MMPROJ_FILE"] == MMPROJ


def test_compose_passes_mmproj_settings_to_the_model_service():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["llm"]["environment"]
    assert "LLM_MMPROJ_FILE=${LLM_MMPROJ_FILE:-}" in env
    #--- Standard: Encoder im RAM, damit die 16-GB-Karte nicht ueberlaeuft.
    assert "LLM_MMPROJ_OFFLOAD=${LLM_MMPROJ_OFFLOAD:-false}" in env


def test_init_stack_adds_mmproj_only_for_the_default_model(tmp_path, monkeypatch):
    init_stack = _load_init_stack(monkeypatch)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    shutil.copy2(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    base = (
        f"ARGUS_WORKSPACE_DIR={(tmp_path / 'ws').as_posix()}\n"
        f"ARGUS_MODELS_DIR={(tmp_path / 'models').as_posix()}\n"
        "SEARXNG_SECRET=a" + "0" * 63 + "\n"
        "CANARY_TOKENS=sk-argus-1234567890abcdef\n"
        "DASHBOARD_SETUP_TOKEN=setup-token-123456789012345\n"
        "ARGUS_ENV_VERSION=2\nCOMPOSE_PROFILES=voice\n"
    )

    def run(extra: str) -> dict:
        (tmp_path / ".env").write_text(base + extra, encoding="utf-8")
        assert init_stack.main(["--stack", str(tmp_path), "--ohne-naechste-schritte"]) == 0
        return init_stack.parse_env((tmp_path / ".env").read_text(encoding="utf-8"))

    #--- Bestehende Installation mit dem Standardmodell: Schluessel kommt dazu.
    env = run("LLM_MODEL_FILE=Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf\n")
    assert env["LLM_MMPROJ_FILE"] == MMPROJ and env["LLM_MMPROJ_OFFLOAD"] == "false"
    #--- Bewusst leer: bleibt leer.
    env = run("LLM_MODEL_FILE=Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf\nLLM_MMPROJ_FILE=\n")
    assert env["LLM_MMPROJ_FILE"] == ""
    #--- Eigenes Modell: die Datei passt nicht, nichts eintragen.
    env = run("LLM_MODEL_FILE=gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf\n")
    assert "LLM_MMPROJ_FILE" not in env
    #--- Ein gesetzter Wert bleibt.
    env = run("LLM_MODEL_FILE=Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf\nLLM_MMPROJ_OFFLOAD=true\n")
    assert env["LLM_MMPROJ_OFFLOAD"] == "true"


def _run_start_script(tmp_path, **env_vars) -> tuple[list[str], str]:
    """llm_start.sh mit Attrappen fuer Schluessel und llama-server ausfuehren; liefert die
    Argumente, mit denen llama-server gestartet worden waere, und stderr."""
    script = (REPO_ROOT / "llm_start.sh").read_text(encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    script = (script
              .replace('KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"', 'KEY="k"')
              .replace("exec /app/llama-server", 'printf "%s\\n"')
              .replace("/models/", f"{models.as_posix()}/"))
    path = tmp_path / "start.sh"
    path.write_text(script, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "LLM_MODEL_FILE": "m.gguf", "OAI_MODEL": "x", **env_vars}
    res = subprocess.run(["sh", str(path)], capture_output=True, text=True, env=env, timeout=10)
    assert res.returncode == 0, res.stderr
    return res.stdout.split("\n"), res.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="braucht sh")
def test_start_script_loads_mmproj_only_when_present(tmp_path):
    args, err = _run_start_script(tmp_path, LLM_MMPROJ_FILE="vision.gguf")
    assert "--mmproj" not in args and "Bildeingabe bleibt aus" in err

    (tmp_path / "models" / "vision.gguf").write_bytes(b"x")
    args, _ = _run_start_script(tmp_path, LLM_MMPROJ_FILE="vision.gguf")
    i = args.index("--mmproj")
    #--- Standard: Encoder im RAM.
    assert args[i + 1].endswith("/vision.gguf") and "--no-mmproj-offload" in args

    args, _ = _run_start_script(tmp_path, LLM_MMPROJ_FILE="vision.gguf", LLM_MMPROJ_OFFLOAD="true")
    assert "--mmproj" in args and "--no-mmproj-offload" not in args

    args, err = _run_start_script(tmp_path)
    assert "--mmproj" not in args and err == ""
