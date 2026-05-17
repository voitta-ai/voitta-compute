#!/usr/bin/env bash
# Build the Voitta menu-bar .app via briefcase.
#
# Lives at the repo root because briefcase writes `build/` and `dist/`
# next to its manifest (also at the repo root). Run it from anywhere:
#
#   ./build_app.sh                # full standalone build (~5 min, ~240 MB)
#   ./build_app.sh --clean        # nuke build/, dist/, wheels/ first
#   ./build_app.sh --package      # also produce a .dmg in dist/ (ad-hoc)
#
# Code-sign + notarise (one-command frictionless distribution):
#
#   ./build_app.sh --package \
#       --sign "Developer ID Application: Roman <Name> (ABCDEF1234)" \
#       --notarize
#
# Output:
#   build/voitta/macos/app/Voitta Bookmarklet.app   # the bundle
#   dist/Voitta Bookmarklet-0.1.0.dmg               # only with --package
#
# After build:
#   open "build/voitta/macos/app/Voitta Bookmarklet.app"
#   # or: backend/.venv/bin/briefcase run macOS app
#
# Distribution friction by mode:
#
#   --package                 — ad-hoc signed. Recipient sees Gatekeeper
#                               warning, must right-click → Open the
#                               first time. Free.
#   --package --sign …        — Developer ID signed. Removes the
#                               "unidentified developer" wording, but
#                               on macOS 10.15+ Gatekeeper still wants
#                               notarisation. Cert is $99/yr Apple
#                               Developer membership.
#   --package --sign … --notarize
#                             — fully Gatekeeper-clean. Recipient
#                               double-clicks, no warning, no friction.
#                               Same $99/yr — notarisation itself is
#                               free + unlimited.
#
# Notarisation prerequisite (one-time):
#
#   xcrun notarytool store-credentials voitta-notary \
#     --apple-id you@example.com --team-id ABCDEF1234
#   # paste the app-specific password from
#   # https://appleid.apple.com → Sign-In and Security
#
#   The keychain profile name "voitta-notary" is what --notarize looks
#   up; override via $VOITTA_NOTARY_PROFILE env var.
#
# Caveats:
#   • mkcert isn't bundled. The .app uses the cert pair currently in
#     backend/certs/ at build time; if you regenerate certs in the
#     repo, rebuild the .app or copy the new pair into
#     ~/Library/Application Support/Voitta Bookmarklet/backend/certs/
#     on the target machine.

set -euo pipefail
cd "$(dirname "$0")"   # repo root
ROOT="$(pwd)"
VENV="$ROOT/backend/.venv"

CLEAN=0
PACKAGE=0
SIGN_IDENTITY=""
NOTARIZE=0
NOTARY_PROFILE="${VOITTA_NOTARY_PROFILE:-voitta-notary}"

# Manual arg loop because --sign takes a value with spaces (e.g.
# "Developer ID Application: Roman <Name> (ABCDEF1234)") and we want
# to validate combinations (e.g. --notarize without --sign is nonsense).
while [ $# -gt 0 ]; do
  case "$1" in
    --clean)    CLEAN=1; shift ;;
    --package)  PACKAGE=1; shift ;;
    --sign)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "[build_app] --sign needs an identity string" >&2
        exit 2
      fi
      SIGN_IDENTITY="$2"; shift 2 ;;
    --notarize) NOTARIZE=1; shift ;;
    -h|--help)
      sed -n '2,55p' "$0"; exit 0 ;;
    *)
      echo "[build_app] unknown arg: $1" >&2
      exit 2 ;;
  esac
done

if [ "$NOTARIZE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  echo "[build_app] --notarize requires --sign \"<Developer ID identity>\" — Apple won't notarise an ad-hoc bundle." >&2
  exit 2
fi
if [ -n "$SIGN_IDENTITY" ] && [ "$PACKAGE" -eq 0 ]; then
  echo "[build_app] --sign without --package has no effect (briefcase only signs at package time). Add --package." >&2
  exit 2
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
         "$ROOT/src/voitta/resources/default_certs" \
         "$ROOT/src/voitta/resources/docs" \
         "$ROOT/src/voitta/resources/rag_scripts" \
         "$ROOT/src/voitta/resources/seed_scripts"
rm -f  "$ROOT/src/voitta/resources/frontend_dist/"* \
       "$ROOT/src/voitta/resources/default_certs/"* 2>/dev/null || true
# docs/ + rag_scripts/ get a recursive wipe — re-staged below so a doc
# rename or build_rag.py edit propagates without --clean.
rm -rf "$ROOT/src/voitta/resources/docs" \
       "$ROOT/src/voitta/resources/rag_scripts" 2>/dev/null || true
mkdir -p "$ROOT/src/voitta/resources/docs" \
         "$ROOT/src/voitta/resources/rag_scripts"
cp -f "$ROOT/frontend/dist/"*.js  "$ROOT/src/voitta/resources/frontend_dist/" 2>/dev/null || true
cp -f "$ROOT/frontend/dist/"*.map "$ROOT/src/voitta/resources/frontend_dist/" 2>/dev/null || true
for f in backend/certs/127.0.0.1+1.pem backend/certs/127.0.0.1+1-key.pem; do
  [ -f "$ROOT/$f" ] && cp -f "$ROOT/$f" "$ROOT/src/voitta/resources/default_certs/"
done
# Project markdown — the RAG indexer at first launch walks this tree.
# Copy with -R so subdirectories (none today, but future-proof) survive.
cp -R "$ROOT/docs/"* "$ROOT/src/voitta/resources/docs/" 2>/dev/null || true
# RAG builder script itself + sibling. ``app.rag_build`` adds this dir
# to sys.path at first launch and imports build_rag from it.
cp -f "$ROOT/rag/build_rag.py"       "$ROOT/src/voitta/resources/rag_scripts/" 2>/dev/null || true
cp -f "$ROOT/rag/build_panel_rag.py" "$ROOT/src/voitta/resources/rag_scripts/" 2>/dev/null || true

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
  # Briefcase signs the .app correctly but its DMG packager runs
  # ``ditto`` to copy the .app into a mounted volume, which fails with
  # ``Operation not permitted`` whenever TCC denies the parent process
  # (Terminal.app / iTerm / Claude Code's harness) the right to write
  # into a freshly-mounted DMG volume. Briefcase swallows the ditto
  # error and writes an empty 80K DMG that still passes signature +
  # notarisation checks (Apple's notary doesn't verify the DMG has a
  # payload). End users get a DMG that opens to a window containing
  # only Applications symlink and .DS_Store — no .app.
  #
  # The workaround: bypass briefcase's DMG step entirely. We sign the
  # .app with briefcase (which works fine — that uses codesign, not
  # ditto-into-DMG), then build the DMG by hand with hdiutil + cp -a.
  # cp -a doesn't preserve the ACLs that confuse macOS's TCC layer, so
  # it copies cleanly where ditto fails.
  #
  # Caveat: the volume name MUST NOT contain spaces during the cp -a
  # phase or cp also fails with EPERM. We rename the volume after the
  # copy, before detach, so the final DMG presents the friendly name.

  if [ -z "$SIGN_IDENTITY" ]; then
    # Ad-hoc — fastest, free, but recipients see Gatekeeper warning.
    # We still need a real DMG, so go through the manual path even
    # without an identity. Sign with --adhoc-sign for the .app only.
    echo "[build_app] briefcase package (.app, ad-hoc sign — no DMG yet)..."
    "$VENV/bin/briefcase" package macOS app --packaging-format zip --adhoc-sign --no-input
  else
    echo "[build_app] briefcase package (.app only, signing as: $SIGN_IDENTITY)..."
    # ``--packaging-format zip`` tells briefcase to skip the broken DMG
    # packager and produce a zip alongside the signed .app. We don't
    # use the zip output but the flag is the documented escape hatch
    # for "sign the .app, skip DMG".
    "$VENV/bin/briefcase" package macOS app \
      --packaging-format zip \
      --identity "$SIGN_IDENTITY" \
      --no-input
  fi

  # ---- Build the real DMG with hdiutil + cp -a -----------------------------
  APP_BUNDLE="$ROOT/build/voitta/macos/app/Voitta Bookmarklet.app"
  if [ ! -d "$APP_BUNDLE" ]; then
    echo "[build_app] post-package .app missing at $APP_BUNDLE" >&2
    exit 1
  fi
  VERSION=$(awk -F'"' '/^version\s*=/ {print $2; exit}' "$ROOT/pyproject.toml")
  DMG="$ROOT/dist/Voitta Bookmarklet-${VERSION}.dmg"
  STAGE_DMG="/tmp/voitta-stage-$$.dmg"
  STAGE_VOL="VoittaBookmarkletStage"   # no spaces — see caveat above
  FINAL_VOL="Voitta Bookmarklet ${VERSION}"

  echo "[build_app] hdiutil create staging DMG..."
  rm -f "$DMG" "$STAGE_DMG"
  mkdir -p "$ROOT/dist"
  hdiutil create -size 400m -fs HFS+ -volname "$STAGE_VOL" -o "$STAGE_DMG" >/dev/null

  echo "[build_app] hdiutil attach + cp -a (bypassing ditto)..."
  hdiutil attach "$STAGE_DMG" -nobrowse >/dev/null
  trap 'hdiutil detach "/Volumes/$STAGE_VOL" 2>/dev/null || hdiutil detach "/Volumes/$FINAL_VOL" 2>/dev/null || true; rm -f "$STAGE_DMG"' EXIT
  cp -a "$APP_BUNDLE" "/Volumes/$STAGE_VOL/"
  ln -s /Applications "/Volumes/$STAGE_VOL/Applications"
  diskutil rename "/Volumes/$STAGE_VOL" "$FINAL_VOL" >/dev/null
  hdiutil detach "/Volumes/$FINAL_VOL" >/dev/null
  trap - EXIT

  echo "[build_app] hdiutil convert to UDZO..."
  hdiutil convert "$STAGE_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG" >/dev/null
  rm -f "$STAGE_DMG"

  if [ -n "$SIGN_IDENTITY" ]; then
    echo "[build_app] codesigning the DMG..."
    codesign --force --sign "$SIGN_IDENTITY" --timestamp "$DMG"

    if [ "$NOTARIZE" -eq 1 ]; then
      echo "[build_app] notarising $DMG (profile: $NOTARY_PROFILE)..."
      # ``--wait`` blocks until Apple's notary service returns Accepted
      # or Invalid. Typical turnaround: 2-15 min.
      if ! xcrun notarytool submit "$DMG" \
           --keychain-profile "$NOTARY_PROFILE" \
           --wait; then
        echo "[build_app] notarytool reported failure." >&2
        echo "[build_app]   (run: xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE)" >&2
        exit 1
      fi
      # Stapling embeds the notary ticket so the DMG passes Gatekeeper
      # offline (without it the recipient's first launch must reach
      # Apple's notary servers; with it, no internet round-trip needed).
      echo "[build_app] stapling notarisation ticket..."
      xcrun stapler staple "$DMG"
      # Verify the staple worked. ``spctl`` is the same tool Gatekeeper
      # uses internally; if it accepts the DMG, end-users will too.
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
