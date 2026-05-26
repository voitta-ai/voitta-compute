#!/usr/bin/env bash
# Build the Voitta Chainlit menu-bar .app via briefcase.
#
# Lives at the repo root because briefcase writes build/ and dist/
# next to its manifest (also at the repo root).
#
#   ./build_app.sh                  # local build, no DMG
#   ./build_app.sh --clean          # nuke build/, dist/, wheels/ first
#   ./build_app.sh --release        # bump patch + package + sign + notarize
#   ./build_app.sh --package        # DMG only, ad-hoc signed
#   ./build_app.sh --bump           # bump patch version only
#
# --release is the one-shot distribution flow:
#   bumps pyproject.toml's patch version, signs with Developer ID,
#   notarises, staples. Defaults to ``Developer ID Application:
#   roman semeine (KU3WTX9RXB)``.
#
# Output:
#   build/voitta-chainlit/macos/app/Voitta Chainlit.app
#   dist/Voitta Chainlit-<VERSION>.dmg   (with --package or --release)
#
# Notarisation prerequisite (one-time):
#   xcrun notarytool store-credentials voitta-notary \
#     --apple-id you@example.com --team-id KU3WTX9RXB

set -euo pipefail
cd "$(dirname "$0")"   # repo root
ROOT="$(pwd)"
VENV="$ROOT/backend/.venv"
PY="$VENV/bin/python"

DEFAULT_SIGN_IDENTITY="Developer ID Application: roman semeine (KU3WTX9RXB)"
NOTARY_PROFILE="${VOITTA_NOTARY_PROFILE:-voitta-notary}"

CLEAN=0
PACKAGE=0
SIGN_IDENTITY=""
NOTARIZE=0
RELEASE=0
BUMP=1

while [ $# -gt 0 ]; do
  case "$1" in
    --clean)    CLEAN=1;   shift ;;
    --package)  PACKAGE=1; shift ;;
    --bump)     BUMP=1;    shift ;;
    --release)
      RELEASE=1; BUMP=1; PACKAGE=1; NOTARIZE=1
      shift ;;
    --sign)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "[build_app] --sign needs an identity string" >&2; exit 2
      fi
      SIGN_IDENTITY="$2"; shift 2 ;;
    --notarize) NOTARIZE=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *)
      echo "[build_app] unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$RELEASE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="$DEFAULT_SIGN_IDENTITY"
fi

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------
if [ ! -d "$VENV" ]; then
  echo "[build_app] backend/.venv not found — run './build.sh' first" >&2
  exit 1
fi
if ! "$PY" -c "import briefcase" 2>/dev/null; then
  echo "[build_app] briefcase not installed — run:" >&2
  echo "  $VENV/bin/pip install 'briefcase>=0.4.1' build" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Optional clean
# ---------------------------------------------------------------------------
if [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] --clean: removing build/ dist/ wheels/"
  rm -rf build dist wheels
fi
rm -rf dist   # always wipe dist; each build gets a uniquely-versioned DMG

# ---------------------------------------------------------------------------
# 2. Bump patch version in pyproject.toml
# ---------------------------------------------------------------------------
if [ "$BUMP" -eq 1 ]; then
  CURRENT_VER=$("$PY" - <<'PYEOF'
import tomllib, re, sys
with open("pyproject.toml", "rb") as f:
    d = tomllib.load(f)
print(d["tool"]["briefcase"]["version"])
PYEOF
)
  NEW_VER=$("$PY" - "$CURRENT_VER" <<'PYEOF'
import sys
parts = sys.argv[1].split(".")
parts[-1] = str(int(parts[-1]) + 1)
print(".".join(parts))
PYEOF
)
  sed -i '' "s/^version = \"$CURRENT_VER\"/version = \"$NEW_VER\"/" pyproject.toml
  echo "[build_app] version bump: $CURRENT_VER → $NEW_VER"
fi

VERSION=$("$PY" - <<'PYEOF'
import tomllib
with open("pyproject.toml", "rb") as f:
    d = tomllib.load(f)
print(d["tool"]["briefcase"]["version"])
PYEOF
)
echo "[build_app] building version $VERSION"

# Stamp the version into the package so the frozen .app can read it at runtime
# without relying on importlib.metadata (which may not be wired up by briefcase).
echo "__version__ = \"$VERSION\"" > "$ROOT/src/voitta_chainlit/_version.py"

# ---------------------------------------------------------------------------
# 3. Build frontend
# ---------------------------------------------------------------------------
echo "[build_app] building frontend…"
cd frontend
if [ ! -d node_modules ]; then
  npm install --silent
fi
npm run build --silent
cd "$ROOT"

# ---------------------------------------------------------------------------
# 4. Pre-build rumps wheel (sdist-only on PyPI; briefcase uses --only-binary)
# ---------------------------------------------------------------------------
WHEELS_DIR="$ROOT/wheels"
mkdir -p "$WHEELS_DIR"
_build_wheel_from_sdist() {
  # Usage: _build_wheel_from_sdist <name-prefix> <pip-spec>
  # Skips if a wheel matching <name-prefix>*.whl already exists (case-insensitive).
  local prefix="$1" spec="$2" sdist src
  # Case-insensitive wheel check (Lazify ships as lazify-*.whl after build).
  if ls "$WHEELS_DIR/"[Ll][Aa][Zz][Ii][Ff][Yy]-*.whl "$WHEELS_DIR/${prefix}"*.whl \
     &>/dev/null 2>&1; then return 0; fi
  # Simpler: just check if any whl whose lowercase name matches prefix exists.
  for f in "$WHEELS_DIR/"*.whl; do
    [ -f "$f" ] || continue
    lower=$(basename "$f" | tr '[:upper:]' '[:lower:]')
    [[ "$lower" == ${prefix}* ]] && return 0
  done
  echo "[build_app] pre-building wheel for $spec…"
  local sdist_dir="$WHEELS_DIR/sdist"
  mkdir -p "$sdist_dir"
  "$VENV/bin/pip" download --no-binary :all: --no-deps -d "$sdist_dir" "$spec" -q
  # Find the sdist case-insensitively (e.g. Lazify-0.4.0.tar.gz).
  sdist=$(find "$sdist_dir" -maxdepth 1 -iname "${prefix}*.tar.gz" 2>/dev/null | head -1)
  if [ -z "$sdist" ]; then
    echo "[build_app] could not download sdist for $spec" >&2; exit 1
  fi
  src="$sdist_dir/src_$$"
  mkdir -p "$src"
  tar -xzf "$sdist" -C "$src" --strip-components=1
  "$PY" -m build --wheel --outdir "$WHEELS_DIR" "$src" -q
  rm -rf "$src"
}

_build_wheel_from_sdist "rumps-"      "rumps>=0.4"
_build_wheel_from_sdist "literalai-"  "literalai==0.1.201"
_build_wheel_from_sdist "syncer-"     "syncer==2.0.3"
_build_wheel_from_sdist "lazify-"     "Lazify==0.4.0"

# ---------------------------------------------------------------------------
# 5. Stage resources into src/voitta_chainlit/resources/
# ---------------------------------------------------------------------------
RES="$ROOT/src/voitta_chainlit/resources"
echo "[build_app] staging resources → $RES"

# 5a. Frontend bundle
rm -rf "$RES/frontend_dist"
cp -r "$ROOT/frontend/dist/" "$RES/frontend_dist/"

# 5b. Docs (small — overwrite)
rm -rf "$RES/docs"
cp -r "$ROOT/docs/" "$RES/docs/"

# 5c. Plugins (overwrite)
rm -rf "$RES/plugins"
cp -r "$ROOT/plugins/" "$RES/plugins/"

# 5d. Screenshot JS libs (html2canvas + html-to-image) — served by
#     /api/_html2canvas.js and /api/_html_to_image.js at runtime.
#     In the frozen .app node_modules/ is absent, so we vendor these
#     two small files into resources/vendor_js/ and serve from there.
mkdir -p "$RES/vendor_js"
NM="$ROOT/frontend/node_modules"
[ -f "$NM/html2canvas/dist/html2canvas.min.js" ] \
  && cp "$NM/html2canvas/dist/html2canvas.min.js" "$RES/vendor_js/html2canvas.min.js" \
  || echo "[build_app] WARNING: html2canvas not in node_modules — skipping"
[ -f "$NM/html-to-image/dist/html-to-image.js" ] \
  && cp "$NM/html-to-image/dist/html-to-image.js" "$RES/vendor_js/html-to-image.js" \
  || echo "[build_app] WARNING: html-to-image not in node_modules — skipping"
echo "[build_app] staged vendor_js"

# 5e. mkcert binary (arm64, required for cert provisioning)
if [ -f "$ROOT/tools/mkcert-arm64" ]; then
  mkdir -p "$RES/bin"
  cp "$ROOT/tools/mkcert-arm64" "$RES/bin/mkcert"
  chmod +x "$RES/bin/mkcert"
elif command -v mkcert &>/dev/null; then
  echo "[build_app] WARNING: tools/mkcert-arm64 not found; users will need brew install mkcert"
fi

# 5e. Code sources version stamp + submodule URLs — used at runtime to
#     shallow-clone the source corpus into the user data dir on first launch.
git submodule status lib-sources > "$RES/code_sources_version.txt" 2>/dev/null || true
# Strip the leading [submodule "…"] path= lines; keep url= for the installer.
cp "$ROOT/.gitmodules" "$RES/gitmodules"
echo "[build_app] staged code_sources_version.txt + gitmodules (no source copy)"

# ---------------------------------------------------------------------------
# 6. Briefcase: create / update / build
# ---------------------------------------------------------------------------
BRIEFCASE="$VENV/bin/briefcase"

echo "[build_app] briefcase update (or create on first run)…"
if [ -d "$ROOT/build" ]; then
  "$BRIEFCASE" update macOS app 2>&1 | grep -v "^$" | sed 's/^/  /'
else
  "$BRIEFCASE" create macOS app 2>&1 | grep -v "^$" | sed 's/^/  /'
fi

echo "[build_app] briefcase build…"
"$BRIEFCASE" build macOS app 2>&1 | grep -v "^$" | sed 's/^/  /'

echo "[build_app] .app built: build/voitta-chainlit/macos/app/Voitta Chainlit.app"

# ---------------------------------------------------------------------------
# 7. Package (DMG) + signing + notarisation
# ---------------------------------------------------------------------------
if [ "$PACKAGE" -eq 0 ]; then
  echo "[build_app] done (no DMG — pass --package or --release to produce one)"
  exit 0
fi

echo "[build_app] briefcase package (DMG)…"
if [ -n "$SIGN_IDENTITY" ]; then
  "$BRIEFCASE" package macOS app \
    --identity "$SIGN_IDENTITY" \
    2>&1 | grep -v "^$" | sed 's/^/  /'
else
  # Ad-hoc sign only (no Developer ID)
  "$BRIEFCASE" package macOS app \
    --adhoc-sign \
    2>&1 | grep -v "^$" | sed 's/^/  /'
fi

DMG=$(ls dist/*.dmg 2>/dev/null | head -1)
if [ -z "$DMG" ]; then
  echo "[build_app] DMG not found under dist/ — package step may have failed" >&2
  exit 1
fi
echo "[build_app] DMG: $DMG"

if [ "$NOTARIZE" -eq 0 ]; then
  echo "[build_app] done (no notarisation — pass --notarize or --release)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 8. Notarise + staple
# ---------------------------------------------------------------------------
if [ -z "$SIGN_IDENTITY" ]; then
  echo "[build_app] --notarize requires --sign or --release" >&2
  exit 1
fi

echo "[build_app] submitting to Apple notarisation (may take 2–15 min)…"
xcrun notarytool submit "$DMG" \
  --keychain-profile "$NOTARY_PROFILE" \
  --wait

echo "[build_app] stapling notarisation ticket…"
# Re-resolve DMG path in case dist/ was recreated during notarisation wait.
DMG=$(ls dist/*.dmg 2>/dev/null | head -1)
if [ -z "$DMG" ]; then
  echo "[build_app] DMG not found after notarisation — staple manually" >&2
  exit 1
fi
xcrun stapler staple "$DMG"

echo "[build_app] verifying…"
spctl -a -vv --type install "$DMG"

echo "[build_app] notarisation complete: $DMG"
