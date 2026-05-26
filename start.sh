#!/usr/bin/env bash
# Single-process serve: FastAPI + Chainlit + built frontend.
# Run ./build.sh first.
set -euo pipefail
cd "$(dirname "$0")/backend"

HOST="${VOITTA_HOST:-127.0.0.1}"
PORT="${VOITTA_PORT:-12358}"
CERT="certs/127.0.0.1+1.pem"
KEY="certs/127.0.0.1+1-key.pem"

if [ ! -d .venv ]; then
  echo "[start.sh] .venv missing — run ./build.sh first" >&2
  exit 1
fi

ARGS=(app.main:app --host "$HOST" --port "$PORT")
if [ "${VOITTA_RELOAD:-0}" = "1" ]; then ARGS+=(--reload); fi

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "[start.sh] https://$HOST:$PORT"
  ARGS+=(--ssl-certfile "$CERT" --ssl-keyfile "$KEY")
else
  echo "[start.sh] http://$HOST:$PORT  (no TLS cert)"
fi

exec ./.venv/bin/uvicorn "${ARGS[@]}"
