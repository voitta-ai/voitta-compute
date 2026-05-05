#!/usr/bin/env bash
# Start the FastAPI backend. All configuration is hardcoded — single source
# of truth is `app/config.py`. We pull HOST / PORT / TLS_CERT_PATH /
# TLS_KEY_PATH from there via `python -c` so the shell script and the
# Python app can never disagree.
#
# HTTPS is auto-detected from the cert files' existence. Generate via
# mkcert (see README "First-time setup"). If they're missing, run.sh
# falls back to HTTP — which won't load on any HTTPS host page
# (mixed-content blocking) but is fine for the dev harness at
# http://localhost:5173.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -e .
fi

# Pull canonical config from app/config.py — one source of truth.
read -r HOST PORT CERT_PATH KEY_PATH <<<"$(./.venv/bin/python - <<'PY'
from app.config import HOST, PORT, TLS_CERT_PATH, TLS_KEY_PATH
print(HOST, PORT, TLS_CERT_PATH, TLS_KEY_PATH)
PY
)"

ARGS=(--host "$HOST" --port "$PORT" --reload)

if [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
  ARGS+=(--ssl-certfile "$CERT_PATH" --ssl-keyfile "$KEY_PATH")
  echo "[run.sh] HTTPS on https://$HOST:$PORT"
else
  echo "[run.sh] HTTP on http://$HOST:$PORT  (cert at $CERT_PATH not found)"
fi

exec ./.venv/bin/uvicorn app.main:app "${ARGS[@]}"
