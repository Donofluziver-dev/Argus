#!/bin/sh
# Startet llama-server als Embedding- oder Reranking-Dienst (EMBED_MODE).
# API-Key wie beim LLM: Fernet-verschluesselt in API_Tokens/sglang_api_key.txt.
set -e
KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"

case "${EMBED_MODE:-}" in
  # bge-m3 liefert den dichten Vektor ueber das CLS-Token.
  embedding) MODE="--embedding --pooling cls" ;;
  reranking) MODE="--reranking" ;;
  *) echo "EMBED_MODE muss embedding oder reranking sein" >&2; exit 1 ;;
esac

# Nicht-kausale Modelle brauchen jede Eingabe vollstaendig in einem Micro-Batch:
# ubatch = ctx = 8192 Token (Hoechstlaenge von bge-m3).
exec /app/llama-server \
  --model "/models/${EMBED_MODEL_FILE}" \
  --alias "${EMBED_ALIAS}" \
  --host 0.0.0.0 --port 8080 \
  --api-key "$KEY" \
  $MODE \
  --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 \
  --parallel 1 \
  --threads "${EMBED_THREADS:-4}" \
  --load-mode none
