"""Installation: Sprachmodell waehlen (Qwen oder Gemma) und nach dem Grafikspeicher aufteilen."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "setup" / "models.json"


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("install_llm", REPO / "setup" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _speicher(auswahl: str) -> dict:
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    return next(m for m in data["models"] if m.get("auswahl") == auswahl)


# ---- Auswahl in models.json -----------------------------------------------------------

def test_two_choices_qwen_is_default(inst):
    assert [m["auswahl"] for m in inst.llm_choices(SPEC)] == ["qwen", "gemma"]
    assert inst.default_llm_choice(SPEC) == "qwen"
    for m in inst.llm_choices(SPEC):
        targets = {f["target"] for f in m["files"]}
        #--- Die .env-Werte zeigen auf Dateien, die die Installation auch laedt.
        for key in ("LLM_MODEL_FILE", "LLM_MTP_MODEL_FILE", "LLM_MMPROJ_FILE"):
            assert not m["env"][key] or m["env"][key] in targets, (m["auswahl"], key)


def test_only_the_chosen_model_is_downloaded(inst):
    def targets(auswahl):
        return {e["target"] for e in inst.load_models_spec(SPEC, auswahl)}

    standard, qwen, gemma, keins = targets(""), targets("qwen"), targets("gemma"), targets(None)
    assert standard == qwen
    assert "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf" in qwen and not any("gemma" in t for t in qwen)
    assert "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf" in gemma and not any("Qwen" in t for t in gemma)
    #--- Ein eigenes Modell: die gemeinsamen Modelle bleiben, ein Sprachmodell laedt keines.
    assert keins == qwen & gemma and "bge-m3-Q8_0.gguf" in keins


def test_choice_is_recognised_from_the_env(inst):
    assert inst.llm_choice_from_env(SPEC, {}) == "qwen"
    assert inst.llm_choice_from_env(SPEC, {"LLM_MODEL_FILE": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf"}) == "gemma"
    assert inst.llm_choice_from_env(SPEC, {"LLM_MODEL_FILE": "eigenes.gguf"}) is None


# ---- Aufteilung nach Grafikspeicher ---------------------------------------------------

def test_16gb_card_gives_the_measured_values(inst):
    #--- RTX 5070 Ti meldet 16303 MiB; gemessen: Qwen 25 Schichten im RAM, Encoder im RAM.
    assert inst.tune_llm_for_gpu(_speicher("qwen")["speicher"], 16303) == {
        "LLM_N_CPU_MOE": "25", "LLM_MMPROJ_OFFLOAD": "false"}
    gemma = inst.tune_llm_for_gpu(_speicher("gemma")["speicher"], 16303)
    assert gemma["LLM_MMPROJ_OFFLOAD"] == "false" and 14 <= int(gemma["LLM_N_CPU_MOE"]) <= 16


@pytest.mark.parametrize("auswahl", ["qwen", "gemma"])
def test_large_card_keeps_everything_on_the_gpu(inst, auswahl):
    #--- RTX 6000 mit 94 GB: alles auf die Karte, auch der Encoder.
    assert inst.tune_llm_for_gpu(_speicher(auswahl)["speicher"], 94 * 1024) == {
        "LLM_N_CPU_MOE": "0", "LLM_MMPROJ_OFFLOAD": "true"}


def test_more_memory_never_means_more_layers_in_ram(inst):
    speicher = _speicher("qwen")["speicher"]
    vorher = 99
    for gb in range(12, 50):
        n = int(inst.tune_llm_for_gpu(speicher, gb * 1024)["LLM_N_CPU_MOE"])
        assert 0 <= n <= 40 and n <= vorher
        vorher = n
    assert inst.tune_llm_for_gpu(speicher, 0) == {}


# ---- Eintragen in die .env ------------------------------------------------------------

def _stack(tmp_path: Path, env_extra: str = "") -> Path:
    (tmp_path / "setup").mkdir()
    shutil.copy2(REPO / "setup" / "init_stack.py", tmp_path / "setup" / "init_stack.py")
    shutil.copy2(REPO / ".env.example", tmp_path / ".env.example")
    (tmp_path / ".env").write_text((REPO / ".env.example").read_text(encoding="utf-8") + env_extra,
                                   encoding="utf-8")
    return tmp_path


def _env(inst, root: Path) -> dict:
    return inst._load_init_stack_module(root).parse_env((root / ".env").read_text(encoding="utf-8"))


def test_new_install_with_qwen_on_a_large_card(inst, tmp_path):
    root = _stack(tmp_path)
    inst.apply_llm_choice(root, _speicher("qwen"), 94 * 1024, modell_gewechselt=False)
    env = _env(inst, root)
    assert env["LLM_N_CPU_MOE"] == "0" and env["LLM_MMPROJ_OFFLOAD"] == "true"
    assert env["LLM_MODEL_FILE"].startswith("Qwen3.6") and "LLM_MTP_MODEL_FILE" not in env


def test_switch_to_gemma_and_back(inst, tmp_path):
    root = _stack(tmp_path)
    inst.apply_llm_choice(root, _speicher("gemma"), 16303, modell_gewechselt=True)
    env = _env(inst, root)
    assert env["LLM_MODEL_FILE"] == "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf"
    assert env["LLM_MTP_MODEL_FILE"] == "mtp-gemma-4-26B-A4B-it.gguf"
    #--- Qwens Bild-Encoder passt nicht zu Gemma und muss raus (Gemmas eigener ist noch
    #--- nicht getestet, Gemma laeuft vorerst ohne Bildeingabe).
    assert env["LLM_MMPROJ_FILE"] == ""
    assert env["OAI_MODEL"] == "gemma-4-26b-a4b" and env["LLM_N_CPU_MOE"] != "25"

    inst.apply_llm_choice(root, _speicher("qwen"), 16303, modell_gewechselt=True)
    env = _env(inst, root)
    assert env["LLM_MODEL_FILE"].startswith("Qwen3.6") and env["LLM_MTP_MODEL_FILE"] == ""
    assert env["LLM_N_CPU_MOE"] == "25" and env["OAI_MODEL"] == "qwen3.6-35b-a3b"
    assert env["LLM_MMPROJ_FILE"] == "mmproj-Qwen3.6-35B-A3B-F16.gguf"


def test_own_values_stay_when_the_model_stays(inst, tmp_path):
    #--- Von Hand angepasste Aufteilung, bewusst ohne Bildeingabe, eigener Alias.
    root = _stack(tmp_path, "\nLLM_N_CPU_MOE=30\nLLM_MMPROJ_FILE=\nOAI_MODEL=mein-modell\n")
    inst.apply_llm_choice(root, _speicher("qwen"), 94 * 1024, modell_gewechselt=False)
    env = _env(inst, root)
    assert env["LLM_N_CPU_MOE"] == "30" and env["LLM_MMPROJ_FILE"] == "" and env["OAI_MODEL"] == "mein-modell"


def test_unknown_llm_option_is_rejected(inst, capsys):
    assert inst.main(["--llm", "llama", "--no-models", "--no-start"]) == 1
    assert "--llm" in capsys.readouterr().out
