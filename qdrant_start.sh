#!/bin/sh
# Startet Qdrant mit dem API-Key aus QDRANT_API_KEY (.env). Qdrant erwartet ihn als
# QDRANT__SERVICE__API_KEY; leer gesetzt wuerde er jede Anfrage ohne Schluessel
# abweisen, deshalb nur mit Wert. /healthz bleibt ohne Schluessel erreichbar.
set -eu
if [ -n "${QDRANT_API_KEY:-}" ]; then
  QDRANT__SERVICE__API_KEY="$QDRANT_API_KEY"
  export QDRANT__SERVICE__API_KEY
fi
unset QDRANT_API_KEY
cd /qdrant
exec ./entrypoint.sh "$@"
