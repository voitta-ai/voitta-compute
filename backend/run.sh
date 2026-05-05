#!/usr/bin/env bash
# Start the FastAPI backend. All configuration is hardcoded — single source
# of truth is `app/config.py`. We pull HOST / PORT / TLS_CERT_PATH /
# TLS_KEY_PATH from there via `python -c` so the shell script and the
# Python app can never disagree.
#
# HTTPS is auto-detected from the cert files' existence. If they're
# missing on first launch, we try to provision them via mkcert (and
# install mkcert via the platform package manager if needed). If that
# fails for any reason — user declines, no supported package manager,
# offline — we fall through to HTTP, which won't load on an HTTPS host
# page (mixed-content blocking) but is fine for the dev harness at
# http://localhost:5173.
set -euo pipefail
cd "$(dirname "$0")"

# ─── cert provisioning helpers ────────────────────────────────────────────
#
# Goal: a fresh-clone, fresh-laptop user can run `./run.sh` and end up
# with a browser-trusted local cert without any README dance.
#
# These helpers print to stderr (>&2) so progress shows up even when
# the script is piped, and they always return 0 on user-decline rather
# than tripping `set -e` — failure is non-fatal; the existing HTTP
# fallback below picks up the slack.

ensure_brew() {
  # macOS only. Returns 0 if brew is on PATH afterwards (or already was).
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  echo >&2
  echo "[run.sh] Homebrew is not installed (needed to install mkcert)." >&2
  echo "[run.sh] The official installer will:" >&2
  echo "          - download and run https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" >&2
  echo "          - prompt for your password (sudo) to write /opt/homebrew" >&2
  echo "          - take a few minutes and modify your shell rc file" >&2
  printf "Install Homebrew now? [y/N] " >&2
  read -r ans </dev/tty || ans=""
  case "$ans" in
    y|Y|yes|YES) ;;
    *)
      echo "[run.sh] Skipping brew install. Install manually: https://brew.sh" >&2
      return 1 ;;
  esac

  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || {
    echo "[run.sh] Homebrew install failed." >&2
    return 1
  }

  # Brew may not be on this shell's PATH yet — installer prints a
  # `eval $(... shellenv)` hint but doesn't apply it for us.
  if ! command -v brew >/dev/null 2>&1; then
    if [ -x /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi
  command -v brew >/dev/null 2>&1
}

ensure_mkcert() {
  if command -v mkcert >/dev/null 2>&1; then
    return 0
  fi
  case "$(uname -s)" in
    Darwin)
      ensure_brew || return 1
      echo "[run.sh] Installing mkcert via brew (incl. nss for Firefox)..." >&2
      brew install mkcert nss || return 1
      ;;
    Linux)
      # Pick the first package manager we recognise. nss-tools/libnss3-tools
      # is needed for mkcert to install its CA into Firefox/Chromium NSS DBs.
      if command -v apt-get >/dev/null 2>&1; then
        echo "[run.sh] Installing mkcert via apt (will prompt for sudo)..." >&2
        sudo apt-get update && sudo apt-get install -y mkcert libnss3-tools || return 1
      elif command -v dnf >/dev/null 2>&1; then
        echo "[run.sh] Installing mkcert via dnf (will prompt for sudo)..." >&2
        sudo dnf install -y mkcert nss-tools || return 1
      elif command -v pacman >/dev/null 2>&1; then
        echo "[run.sh] Installing mkcert via pacman (will prompt for sudo)..." >&2
        sudo pacman -S --needed --noconfirm mkcert nss || return 1
      else
        echo "[run.sh] No supported package manager found (apt/dnf/pacman)." >&2
        echo "[run.sh] Install mkcert manually: https://github.com/FiloSottile/mkcert" >&2
        return 1
      fi
      ;;
    *)
      echo "[run.sh] Unsupported platform: $(uname -s). Install mkcert manually:" >&2
      echo "          https://github.com/FiloSottile/mkcert" >&2
      return 1
      ;;
  esac
  command -v mkcert >/dev/null 2>&1
}

ensure_cert() {
  # If both files exist already, we're done.
  if [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
    return 0
  fi
  echo "[run.sh] No TLS cert at $CERT_PATH — provisioning via mkcert." >&2

  ensure_mkcert || {
    echo "[run.sh] Couldn't install mkcert; skipping cert provisioning." >&2
    return 1
  }

  local certs_dir
  certs_dir="$(dirname "$CERT_PATH")"
  mkdir -p "$certs_dir"

  # mkcert -install: writes the local CA into the OS trust store.
  # macOS: uses `security` and prompts via keychain UI (Touch ID / pwd).
  # Linux: mkcert internally invokes sudo for update-ca-certificates /
  # update-ca-trust. So we DON'T prefix with sudo here.
  echo "[run.sh] Installing local CA into system trust store" >&2
  echo "          (may prompt for your password / keychain auth)..." >&2
  if ! mkcert -install; then
    echo "[run.sh] mkcert -install failed — the cert will be generated but" >&2
    echo "          your browser won't trust it. Re-run after fixing." >&2
    return 1
  fi

  # Generate the leaf cert in $certs_dir. mkcert writes
  # ./127.0.0.1+1.pem and ./127.0.0.1+1-key.pem — exactly the names
  # app/config.py:_detect_cert_pair() looks for as the preferred pair.
  echo "[run.sh] Generating cert for 127.0.0.1, localhost..." >&2
  if ! ( cd "$certs_dir" && mkcert 127.0.0.1 localhost ); then
    echo "[run.sh] mkcert generation failed." >&2
    return 1
  fi

  echo "[run.sh] Cert provisioned at $CERT_PATH" >&2
}

# ─── main ────────────────────────────────────────────────────────────────

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

# Best-effort: if cert is missing, try to provision it. Failure here
# falls through to the HTTP fallback below — explicit, never aborts.
ensure_cert || true

ARGS=(--host "$HOST" --port "$PORT" --reload)

if [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
  ARGS+=(--ssl-certfile "$CERT_PATH" --ssl-keyfile "$KEY_PATH")
  echo "[run.sh] HTTPS on https://$HOST:$PORT"
else
  echo "[run.sh] HTTP on http://$HOST:$PORT  (cert at $CERT_PATH not found)"
fi

exec ./.venv/bin/uvicorn app.main:app "${ARGS[@]}"
