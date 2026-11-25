#!/bin/sh
# Startet llama-server mit den Werten aus .env. Der API-Key liegt
# Fernet-verschluesselt in API_Tokens/llm_api_key.txt.
set -e
KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"

SPEC=""
if [ "${LLM_MTP:-false}" = "true" ]; then
  SPEC="--spec-type draft-mtp --spec-draft-n-max ${LLM_MTP_DRAFT:-2}"
  # Qwen3.6 traegt den MTP-Kopf in der Modelldatei. Andere Modelle (Gemma 4)
  # bringen ihn als eigene Datei mit; sie liegt im selben Modellordner.
  if [ -n "${LLM_MTP_MODEL_FILE:-}" ]; then
    SPEC="$SPEC --spec-draft-model /models/${LLM_MTP_MODEL_FILE}"
  fi
fi

# Obergrenze je Platz im gemeinsamen KV-Pool: Chat und Code laufen parallel,
# keiner soll den anderen ganz verdraengen.
PER_SLOT=""
if [ -n "${LLM_CTX_PER_SLOT:-}" ]; then
  PER_SLOT="--kv-unified-per-slot ${LLM_CTX_PER_SLOT}"
fi

# Bildeingabe: Die Zusatzdatei (mmproj) bringt den Bild-Encoder des Modells. Fehlt
# sie, startet das Modell ohne Bildeingabe statt gar nicht; das Backend fragt /props.
# Standard: Encoder im RAM (~1 GB VRAM gespart, ein Bild ~11 statt ~5 s auf der
# 16-GB-Karte). LLM_MMPROJ_OFFLOAD=true legt ihn auf die Grafikkarte.
VISION=""
if [ -n "${LLM_MMPROJ_FILE:-}" ]; then
  if [ -f "/models/${LLM_MMPROJ_FILE}" ]; then
    VISION="--mmproj /models/${LLM_MMPROJ_FILE}"
    if [ "${LLM_MMPROJ_OFFLOAD:-false}" != "true" ]; then
      VISION="$VISION --no-mmproj-offload"
    fi
  else
    echo "WARNUNG: /models/${LLM_MMPROJ_FILE} fehlt, Bildeingabe bleibt aus." >&2
  fi
fi

# --load-mode none (kein mmap): das Modell liegt auf einem Windows-Bind-Mount;
# mmap wuerde ausgelagerte Experten ueber das langsame Dateisystem nachladen.
exec /app/llama-server \
  --model "/models/${LLM_MODEL_FILE}" \
  --alias "${OAI_MODEL}" \
  --host 0.0.0.0 --port 8080 \
  --api-key "$KEY" \
  --n-gpu-layers 99 \
  --n-cpu-moe "${LLM_N_CPU_MOE:-16}" \
  --ctx-size "${LLM_CTX_SIZE:-102400}" \
  --parallel "${LLM_PARALLEL:-2}" --kv-unified $PER_SLOT \
  --flash-attn on \
  --cache-type-k "${LLM_CACHE_TYPE:-f16}" --cache-type-v "${LLM_CACHE_TYPE:-f16}" \
  --batch-size "${LLM_BATCH:-2048}" --ubatch-size "${LLM_UBATCH:-1024}" \
  --threads "${LLM_THREADS:-8}" \
  --jinja --reasoning-format deepseek \
  --load-mode none \
  --metrics \
  $SPEC $VISION
