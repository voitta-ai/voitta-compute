#!/usr/bin/env bash
# Build the Voitta menu-bar .app via briefcase.
#
# Lives at the repo root because briefcase writes `build/` and `dist/`
# next to its manifest (also at the repo root). Modelled on
# ~/DEVEL/voitta-desktop/scripts/build_app.sh — same flow, no custom
# DMG hand-rolling.
#
#   ./build_app.sh                       # local build, no DMG
#   ./build_app.sh --clean               # nuke build/, dist/, wheels/ first
#   ./build_app.sh --release             # bump patch + package + sign + notarize
#   ./build_app.sh --package             # DMG only, ad-hoc signed
#
# --release is the one-shot distribution flow. It runs everything an
# end-user-shippable DMG needs: bumps pyproject.toml's patch version,
# signs with the Developer ID identity, notarises, staples. Defaults
# to ``Developer ID Application: roman semeine (KU3WTX9RXB)`` —
# override with --sign "<identity>".
#
# Output:
#   build/voitta/macos/app/Voitta Bookmarklet.app   # the bundle
#   dist/Voitta Bookmarklet-<VERSION>.dmg           # with --package or --release
#
# Notarisation prerequisite (one-time):
#   xcrun notarytool store-credentials voitta-notary \
#     --apple-id you@example.com --team-id KU3WTX9RXB
#   # The keychain profile name "voitta-notary" is what --notarize
#   # looks up; override via $VOITTA_NOTARY_PROFILE env var.

set -euo pipefail
cd "$(dirname "$0")"   # repo root
ROOT="$(pwd)"
VENV="$ROOT/backend/.venv"

# Default Developer ID — same identity used by voitta-desktop. Override
# via --sign "<identity>" or by editing this constant.
DEFAULT_SIGN_IDENTITY="Developer ID Application: roman semeine (KU3WTX9RXB)"

CLEAN=0
PACKAGE=0
SIGN_IDENTITY=""
NOTARIZE=0
RELEASE=0
BUMP=0
NOTARY_PROFILE="${VOITTA_NOTARY_PROFILE:-voitta-notary}"

while [ $# -gt 0 ]; do
  case "$1" in
    --clean)    CLEAN=1; shift ;;
    --package)  PACKAGE=1; shift ;;
    --bump)     BUMP=1; shift ;;
    --release)
      # One-shot: bump + package + sign + notarize with defaults.
      RELEASE=1; BUMP=1; PACKAGE=1; NOTARIZE=1
      shift ;;
    --sign)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "[build_app] --sign needs an identity string" >&2
        exit 2
      fi
      SIGN_IDENTITY="$2"; shift 2 ;;
    --notarize) NOTARIZE=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *)
      echo "[build_app] unknown arg: $1" >&2
      exit 2 ;;
  esac
done

# --release implies signing with the default Developer ID identity
# unless --sign overrides it.
if [ "$RELEASE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="$DEFAULT_SIGN_IDENTITY"
fi

if [ "$NOTARIZE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  echo "[build_app] --notarize requires --sign (or --release). Apple won't notarise an ad-hoc bundle." >&2
  exit 2
fi
if [ -n "$SIGN_IDENTITY" ] && [ "$PACKAGE" -eq 0 ]; then
  echo "[build_app] --sign without --package has no effect. Add --package or use --release." >&2
  exit 2
fi

# Version bump: increment the patch number in pyproject.toml so each
# build produces a uniquely-versioned DMG (and so the new in-app
# deploy-stamp check actually triggers on every shipped binary).
# Leaves the change as an uncommitted diff — user commits when ready.
if [ "$BUMP" -eq 1 ]; then
  OLD_VER=$(awk -F'"' '/^version[[:space:]]*=/ {print $2; exit}' "$ROOT/pyproject.toml")
  if [ -z "$OLD_VER" ]; then
    echo "[build_app] couldn't read current version from pyproject.toml" >&2
    exit 1
  fi
  IFS='.' read -r VMAJ VMIN VPATCH <<< "$OLD_VER"
  NEW_VER="${VMAJ}.${VMIN}.$((VPATCH + 1))"
  /usr/bin/sed -i '' -E "s/^version = \"[^\"]+\"/version = \"${NEW_VER}\"/" "$ROOT/pyproject.toml"
  echo "[build_app] version bumped: ${OLD_VER} → ${NEW_VER}"
fi

if [ ! -d "$VENV" ]; then
  echo "[build_app] no backend/.venv found — run backend/run.sh once to bootstrap" >&2
  exit 1
fi

# Make sure desktop deps (briefcase, pyobjc, …) are installed in the
# backend venv. `pip install -e` is run from inside backend/ because
# that's where backend's pyproject.toml (with the [desktop] extras) lives.
echo "[build_app] ensuring [desktop] extras..."
( cd "$ROOT/backend" && "$VENV/bin/pip" install -q -e ".[desktop]" )

# Build the frontend if it isn't already built. The .app embeds
# frontend/dist/widget.js; without it the bookmarklet has nothing
# to load on the user's first connection.
if [ ! -f "$ROOT/frontend/dist/widget.js" ]; then
  echo "[build_app] frontend/dist/widget.js missing — building it..."
  ( cd "$ROOT/frontend" && npm install --silent && npm run build )
fi

if [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] cleaning build/, dist/, wheels/..."
  rm -rf "$ROOT/build" "$ROOT/dist" "$ROOT/wheels"
fi

# Generate the .app icon if it isn't already there. Tiny script; cheap
# to re-run, but skipping when the .icns is present means iconutil
# isn't a hard dependency of incremental builds.
if [ ! -f "$ROOT/icons/voitta.icns" ]; then
  echo "[build_app] generating icons/voitta.icns..."
  "$VENV/bin/python" "$ROOT/tools/generate_icon.py"
fi

# rumps is sdist-only on PyPI; briefcase passes --only-binary :all: to
# pip so we pre-build a wheel locally and point at it via a relative
# file path in the briefcase requires list.
mkdir -p "$ROOT/wheels"
if ! ls "$ROOT/wheels"/rumps-*.whl >/dev/null 2>&1; then
  echo "[build_app] pre-building rumps wheel..."
  "$VENV/bin/pip" wheel --no-deps --quiet -w "$ROOT/wheels" rumps
fi

# Stage frontend bundle + certs into src/voitta/resources/ so briefcase
# auto-includes them via package data. Sync each build so a frontend
# rebuild propagates without `--clean`.
echo "[build_app] staging resources into src/voitta/resources/..."
mkdir -p "$ROOT/src/voitta/resources/frontend_dist" \
         "$ROOT/src/voitta/resources/bin" \
         "$ROOT/src/voitta/resources/docs" \
         "$ROOT/src/voitta/resources/rag_scripts" \
         "$ROOT/src/voitta/resources/seed_scripts"
rm -f  "$ROOT/src/voitta/resources/frontend_dist/"* \
       "$ROOT/src/voitta/resources/bin/"* 2>/dev/null || true
# docs/ + rag_scripts/ get a recursive wipe — re-staged below so a doc
# rename or build_rag.py edit propagates without --clean.
rm -rf "$ROOT/src/voitta/resources/docs" \
       "$ROOT/src/voitta/resources/rag_scripts" 2>/dev/null || true
mkdir -p "$ROOT/src/voitta/resources/docs" \
         "$ROOT/src/voitta/resources/rag_scripts"
cp -f "$ROOT/frontend/dist/"*.js  "$ROOT/src/voitta/resources/frontend_dist/" 2>/dev/null || true
cp -f "$ROOT/frontend/dist/"*.map "$ROOT/src/voitta/resources/frontend_dist/" 2>/dev/null || true
# Bundle mkcert so end users don't need to ``brew install mkcert``.
# ``app.certs._mkcert_path`` prefers this binary over PATH so first
# launch can run ``mkcert -install`` + issue a localhost cert pair
# right inside the user's own trust store. ~4 MB arm64 Mach-O.
# Briefcase re-signs every Mach-O in the bundle at package time so
# notarisation accepts it under our Developer ID.
MKCERT_SRC=$(command -v mkcert || true)
if [ -z "$MKCERT_SRC" ] || [ ! -f "$MKCERT_SRC" ]; then
  echo "[build_app] mkcert not found on PATH — install with: brew install mkcert" >&2
  exit 1
fi
cp -f "$MKCERT_SRC" "$ROOT/src/voitta/resources/bin/mkcert"
chmod +x "$ROOT/src/voitta/resources/bin/mkcert"
# Project markdown — the RAG indexer at first launch walks this tree.
# Copy with -R so subdirectories (none today, but future-proof) survive.
cp -R "$ROOT/docs/"* "$ROOT/src/voitta/resources/docs/" 2>/dev/null || true
# RAG builder script. ``app.rag_build`` adds this dir to sys.path at
# first launch and drives build_rag.main() to materialise the index.
if [ ! -f "$ROOT/scripts/build_rag.py" ]; then
  echo "[build_app] scripts/build_rag.py missing — RAG won't work in the bundle" >&2
  exit 1
fi
cp -f "$ROOT/scripts/build_rag.py" "$ROOT/src/voitta/resources/rag_scripts/"

# Bookmarklet source — the "Copy bookmark text" menu item reads this
# file at runtime, minifies it, and writes the resulting javascript:
# URL to the clipboard. Without it, the menu item shows an error.
rm -rf "$ROOT/src/voitta/resources/bookmarklet" 2>/dev/null || true
mkdir -p "$ROOT/src/voitta/resources/bookmarklet"
cp -f "$ROOT/bookmarklet/bookmarklet.js" "$ROOT/src/voitta/resources/bookmarklet/" 2>/dev/null || true

# Seed scripts — the curated compute+report pairs the agent calls
# automatically (a4db_parse / a4db_3d / dat_parse / dat_curves). They
# get copied out of the bundle into PROJECT_ROOT/python_storage/ at
# first launch so they live in a user-writable directory (the bundle
# is read-only). Track the names explicitly rather than copying every
# python_storage/{compute,reports}/ entry: most local scripts are
# dev-only experiments and don't belong in a redistributable bundle.
rm -rf "$ROOT/src/voitta/resources/seed_scripts" 2>/dev/null || true
mkdir -p "$ROOT/src/voitta/resources/seed_scripts/compute" \
         "$ROOT/src/voitta/resources/seed_scripts/reports"
for s in a4db_parse dat_parse; do
  if [ -f "$ROOT/python_storage/compute/$s/code.py" ]; then
    cp -R "$ROOT/python_storage/compute/$s" "$ROOT/src/voitta/resources/seed_scripts/compute/"
  fi
done
for s in a4db_3d dat_curves; do
  if [ -f "$ROOT/python_storage/reports/$s/code.py" ]; then
    cp -R "$ROOT/python_storage/reports/$s" "$ROOT/src/voitta/resources/seed_scripts/reports/"
  fi
done
# Plugins. Each plugin's whole tree gets staged into the bundle so the
# packaged .app can run their backend Python (sys.path injection at
# first launch), bundle their frontend widget.ts (already done at
# build time via Vite glob), and index their docs into RAG. Whatever's
# in $ROOT/plugins is copied verbatim — gitignore only governs what
# the OSS REPO tracks; the bundle ships whatever the local tree has.
rm -rf "$ROOT/src/voitta/resources/plugins" 2>/dev/null || true
if [ -d "$ROOT/plugins" ]; then
  mkdir -p "$ROOT/src/voitta/resources/plugins"
  cp -R "$ROOT/plugins/." "$ROOT/src/voitta/resources/plugins/"
fi

# Plugin seed scripts — same idea as core seed_scripts, but pulled
# from each plugin's seed_scripts/{compute,reports}/ (plugins ship
# their seeds under that path; they're copied into the user's
# python_storage/ at first launch alongside the core seeds). Extra
# subdirectories (e.g. nested docs of compute scripts) are preserved.
for plugin_dir in "$ROOT/plugins"/*; do
  [ -d "$plugin_dir" ] || continue
  for kind in compute reports; do
    src="$plugin_dir/seed_scripts/$kind"
    [ -d "$src" ] || continue
    for entry in "$src"/*; do
      [ -d "$entry" ] || continue
      cp -R "$entry" "$ROOT/src/voitta/resources/seed_scripts/$kind/"
    done
  done
done

# Strip the per-run state out of the staged copies — meta.json's
# last_run fields are dev-machine artefacts and shouldn't ship.
find "$ROOT/src/voitta/resources/seed_scripts" -type d -name "runs" -prune -exec rm -rf {} +
find "$ROOT/src/voitta/resources/seed_scripts" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$ROOT/src/voitta/resources/plugins" -type d -name "__pycache__" -prune -exec rm -rf {} +

# briefcase create — downloads CPython support package, installs deps
# into the bundle's standalone Python (idempotent if no spec changes).
if [ ! -d "$ROOT/build/voitta/macos/app/Voitta Bookmarklet.app" ] || [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] briefcase create..."
  "$VENV/bin/briefcase" create macOS app --no-input
fi

# briefcase update — re-stages source + resources without re-installing
# wheels. Cheap; keeps the bundle in sync with our local edits.
echo "[build_app] briefcase update (sync source + resources)..."
"$VENV/bin/briefcase" update macOS app --no-input

# briefcase build — strips, signs (ad-hoc by default).
echo "[build_app] briefcase build (ad-hoc sign)..."
"$VENV/bin/briefcase" build macOS app --no-input

APP="build/voitta/macos/app/Voitta Bookmarklet.app"
if [ ! -d "$APP" ]; then
  echo "[build_app] briefcase reported success but $APP is missing." >&2
  exit 1
fi

if [ "$PACKAGE" -eq 1 ]; then
  if [ -z "$SIGN_IDENTITY" ]; then
    echo "[build_app] briefcase package (DMG, ad-hoc sign)..."
    "$VENV/bin/briefcase" package macOS app --adhoc-sign --no-input
  else
    echo "[build_app] briefcase package — signing as: $SIGN_IDENTITY"
    "$VENV/bin/briefcase" package macOS app \
      --identity "$SIGN_IDENTITY" \
      --no-input

    if [ "$NOTARIZE" -eq 1 ]; then
      DMG=$(ls -1 "$ROOT/dist/"*.dmg 2>/dev/null | head -1)
      if [ -z "$DMG" ]; then
        echo "[build_app] expected a .dmg under dist/ but none found." >&2
        exit 1
      fi
      echo "[build_app] notarising $DMG (profile: $NOTARY_PROFILE)..."
      if ! xcrun notarytool submit "$DMG" \
           --keychain-profile "$NOTARY_PROFILE" \
           --wait; then
        echo "[build_app] notarytool reported failure." >&2
        echo "[build_app]   (run: xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE)" >&2
        exit 1
      fi
      echo "[build_app] stapling notarisation ticket..."
      xcrun stapler staple "$DMG"
      echo "[build_app] verifying with spctl..."
      spctl -a -vv --type install "$DMG" || true
    fi
  fi
fi

echo
echo "[build_app] done."
echo "    $ROOT/$APP"
du -sh "$APP" 2>/dev/null | sed 's/^/    size  /'
if [ "$PACKAGE" -eq 1 ]; then
  ls -1 dist/*.dmg 2>/dev/null | sed 's/^/    dmg   /'
fi
echo
# Quote: the bundle name contains a space.
echo "    open \"$ROOT/$APP\""
echo "    backend/.venv/bin/briefcase run macOS app   # alt: streams stdout/stderr"
