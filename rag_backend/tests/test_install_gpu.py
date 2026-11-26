"""Installation: Dokumentensuche und Sprache auf der Grafikkarte oder der CPU."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "setup" / "models.json"


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("install_gpu", REPO / "setup" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _qwen() -> dict:
    return next(m for m in json.loads(SPEC.read_text(encoding="utf-8"))["models"] if m.get("auswahl") == "qwen")


# ---- Vorschlag nach Grafikspeicher ----------------------------------------------------

def test_small_card_keeps_search_and_speech_on_the_cpu(inst):
    assert inst.default_gpu_groups(16303, _qwen(), use_voice=True) == []
    assert inst.default_gpu_groups(0, _qwen(), use_voice=True) == []


def test_large_card_puts_both_on_the_gpu(inst):
    #--- RTX 6000 mit 94 GB: Sprachmodell ganz auf der Karte, und Suche und Sprache passen dazu.
    assert inst.default_gpu_groups(94 * 1024, _qwen(), use_voice=True) == ["suche", "sprache"]
    assert inst.default_gpu_groups(94 * 1024, _qwen(), use_voice=False) == ["suche"]


def test_suggestion_only_when_the_model_still_fits_entirely(inst):
    need = inst.llm_need_gb(_qwen()) + inst._VRAM_RESERVE_GB
    knapp = int((need + inst.GPU_GRUPPEN["suche"]["gb"] - 0.1) * 1024)
    reicht = int((need + inst.GPU_GRUPPEN["suche"]["gb"] + 0.1) * 1024)
    assert inst.default_gpu_groups(knapp, _qwen(), use_voice=False) == []
    assert inst.default_gpu_groups(reicht, _qwen(), use_voice=False) == ["suche"]


# ---- COMPOSE_FILE ---------------------------------------------------------------------

def test_compose_file_value(inst):
    assert inst.compose_file_value({}, []) is None
    assert inst.compose_file_value({}, ["sprache", "suche"]) == \
        "docker-compose.yml:compose/gpu-suche.yml:compose/gpu-sprache.yml"
    #--- Das Override des Offline-Pakets bleibt zuletzt, damit es die Image-Namen festlegt.
    paket = {"COMPOSE_PATH_SEPARATOR": ":", "COMPOSE_FILE": "docker-compose.yml:docker-compose.bundle.yml"}
    assert inst.compose_file_value(paket, ["suche"]) == \
        "docker-compose.yml:compose/gpu-suche.yml:docker-compose.bundle.yml"
    #--- Zurueck auf die CPU: die GPU-Datei faellt weg, der Rest bleibt.
    gpu = {"COMPOSE_PATH_SEPARATOR": ":", "COMPOSE_FILE": "docker-compose.yml:compose/gpu-suche.yml"}
    assert inst.compose_file_value(gpu, []) == "docker-compose.yml"
    assert inst.gpu_groups_from_env(gpu) == ["suche"]
    #--- Ohne eigenen Trenner gilt unter Windows ';'.
    if inst.os.name == "nt":
        assert inst.gpu_groups_from_env({"COMPOSE_FILE": "docker-compose.yml;compose/gpu-sprache.yml"}) == ["sprache"]


def test_set_gpu_groups_writes_the_env(inst, tmp_path):
    (tmp_path / "setup").mkdir()
    shutil.copy2(REPO / "setup" / "init_stack.py", tmp_path / "setup" / "init_stack.py")
    shutil.copy2(REPO / ".env.example", tmp_path / ".env.example")
    shutil.copy2(REPO / ".env.example", tmp_path / ".env")
    inst.set_gpu_groups(tmp_path, [])
    assert "COMPOSE_FILE" not in (tmp_path / ".env").read_text(encoding="utf-8")
    inst.set_gpu_groups(tmp_path, ["suche", "sprache"])
    env = inst._load_init_stack_module(tmp_path).parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env["COMPOSE_PATH_SEPARATOR"] == ":"
    assert inst.gpu_groups_from_env(env) == ["suche", "sprache"]


# ---- Compose-Dateien und Startskript --------------------------------------------------

@pytest.mark.parametrize("gruppe", ["suche", "sprache"])
def test_gpu_compose_files_extend_existing_services(inst, gruppe):
    basis = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    datei = yaml.safe_load((REPO / inst.GPU_GRUPPEN[gruppe]["datei"]).read_text(encoding="utf-8"))
    for name, svc in datei["services"].items():
        assert name in basis
        assert "-gpu:" in svc["image"]                               # eigener Name, kein Ueberschreiben
        geraet = svc["deploy"]["resources"]["reservations"]["devices"][0]
        assert geraet["driver"] == "nvidia" and "gpu" in geraet["capabilities"]
        #--- Basis-Images per Digest festgelegt.
        for wert in (svc.get("build", {}).get("args") or {}).values():
            if "/" in str(wert):
                assert "@sha256:" in wert


@pytest.mark.skipif(shutil.which("sh") is None, reason="braucht sh")
def test_embed_start_script_uses_the_gpu_only_when_asked(tmp_path):
    script = (REPO / "embed_start.sh").read_text(encoding="utf-8")
    script = (script.replace('KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"', 'KEY="k"')
              .replace("exec /app/llama-server", 'printf "%s\\n"'))
    path = tmp_path / "start.sh"
    path.write_text(script, encoding="utf-8")

    def args(**env):
        res = subprocess.run(["sh", str(path)], capture_output=True, text=True, timeout=10,
                             env={"PATH": "/usr/bin:/bin", "EMBED_MODE": "embedding",
                                  "EMBED_MODEL_FILE": "m.gguf", "EMBED_ALIAS": "a", **env})
        assert res.returncode == 0, res.stderr
        return res.stdout.split("\n")

    assert "--n-gpu-layers" not in args()
    gpu = args(EMBED_GPU_LAYERS="99")
    assert gpu[gpu.index("--n-gpu-layers") + 1] == "99"


def test_speech_recognition_device_comes_from_the_environment():
    app = (REPO / "stt_service" / "app.py").read_text(encoding="utf-8")
    assert 'DEVICE = os.getenv("WHISPER_DEVICE", "cpu")' in app and "device=DEVICE" in app
    sprache = yaml.safe_load((REPO / "compose" / "gpu-sprache.yml").read_text(encoding="utf-8"))
    assert "WHISPER_DEVICE=cuda" in sprache["services"]["stt-service"]["environment"]


def test_unknown_gpu_option_is_rejected(inst):
    with pytest.raises(SystemExit):
        inst.main(["--suche", "beides", "--no-models", "--no-start"])
