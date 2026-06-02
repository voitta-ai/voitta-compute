#!/usr/bin/env bash
# One-shot Linux/server bring-up: data path → submodules → build → RAG → serve.
#
# Unlike start.sh (dev/loopback, auto-TLS if certs exist) this is meant for a
# headless server behind a reverse proxy: it forces PLAIN HTTP and binds the
# single app process. Put TLS termination (nginx/caddy/etc.) in front.
#
# Everything is idempotent — re-running skips work that's already done:
#   • submodules: git no-ops if already checked out
#   • build.sh:   npm/pip are incremental
#   • RAG:        skipped when app.rag_build.is_built() is true (use --rebuild-rag to force)
#
# Override anything via env:
#   VOITTA_DATA_ROOT   where mutable data lives   (default: ~/.local/share/voitta)
#   VOITTA_HOST        bind address               (default: 127.0.0.1)
#   VOITTA_PORT        bind port                  (default: 12358)
#
# Flags:
#   --rebuild-rag      force a RAG rebuild even if indexes exist
#   --skip-build       skip submodules + build.sh (just (maybe) RAG + serve)
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (env-overridable) ---------------------------------------------
export VOITTA_DATA_ROOT="${VOITTA_DATA_ROOT:-$HOME/.local/share/voitta}"
HOST="${VOITTA_HOST:-127.0.0.1}"
PORT="${VOITTA_PORT:-12358}"

REBUILD_RAG=0
SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild-rag) REBUILD_RAG=1 ;;
    --skip-build)  SKIP_BUILD=1 ;;
    *) echo "[server-start] unknown flag: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$VOITTA_DATA_ROOT"
echo "[server-start] VOITTA_DATA_ROOT=$VOITTA_DATA_ROOT"

# ---- 1. submodules + build -------------------------------------------------
if [ "$SKIP_BUILD" = "0" ]; then
  echo "[server-start] submodules (lib-sources/*)"
  git submodule update --init --recursive --depth 1

  echo "[server-start] build.sh (frontend bundle + backend venv)"
  ./build.sh
else
  echo "[server-start] --skip-build: skipping submodules + build.sh"
fi

PY="$(pwd)/backend/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[server-start] $PY missing — run without --skip-build first" >&2
  exit 1
fi

# ---- 2. RAG indexes (idempotent) ------------------------------------------
# is_built() lives in app.rag_build; run from backend/ so app.* imports resolve.
RAG_BUILT="$( (cd backend && "$PY" -c 'from app.rag_build import is_built; print(int(is_built()))') 2>/dev/null || echo 0)"

if [ "$REBUILD_RAG" = "1" ] || [ "$RAG_BUILT" != "1" ]; then
  echo "[server-start] building RAG indexes (this can take ~1 min for the code corpus)"
  "$PY" scripts/build_rag.py
else
  echo "[server-start] RAG indexes already built — skipping (use --rebuild-rag to force)"
fi

# ---- 3. serve (plain HTTP, single process) --------------------------------
# Mirror start.sh's known-good layout: run from backend/ so chainlit + app.*
# relative paths resolve exactly as they do in dev.
echo "[server-start] http://$HOST:$PORT  (plain HTTP — terminate TLS upstream)"
cd backend
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
