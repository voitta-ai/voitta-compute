#!/usr/bin/env bash
# Build the frontend into frontend/dist and install backend deps.
# The backend serves the built frontend at / and Chainlit at /chainlit.
set -euo pipefail
cd "$(dirname "$0")"

echo "[build.sh] frontend → frontend/dist"
( cd frontend && \
  (command -v npm >/dev/null || { echo "npm required"; exit 1; }) && \
  npm install --silent && \
  npm run build )

echo "[build.sh] backend venv"
( cd backend && \
  if [ ! -d .venv ]; then python3 -m venv .venv; fi && \
  ./.venv/bin/pip install -e . )

echo "[build.sh] done."
