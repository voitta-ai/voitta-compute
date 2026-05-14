# Building the signed `.dmg` — what works, what doesn't

This document is the field guide for `build_app.sh`. It records the
exact failure modes seen during macOS packaging and the workarounds
that produced a Gatekeeper-clean DMG. Keep it next to the script —
briefcase's packaging step is fragile and the trip-wires aren't
visible in the script's success path.

Substitute the placeholders below with your own values:
- `<DEV_IDENTITY>` — full Developer ID string, e.g.
  `Developer ID Application: <Your Name> (XXXXXXXXXX)`.
- `<TEAM_ID>` — your 10-character Apple team ID.
- `<NOTARY_PROFILE>` — keychain profile name created via
  `xcrun notarytool store-credentials`; the script defaults to
  `voitta-notary`, override with the `VOITTA_NOTARY_PROFILE` env var.

---

## 1. The happy-path command

```
./build_app.sh --package \
    --sign "<DEV_IDENTITY>" \
    --notarize
```

When this runs end-to-end it produces two artefacts:

| Artefact | Path | Size |
| -------- | ---- | ---- |
| Bundle   | `build/voitta/macos/app/Voitta Bookmarklet.app` | ~238 MB |
| Disk image | `dist/Voitta Bookmarklet-0.1.0.dmg` | ~61 MB |

End-state verification on a healthy build:

```
$ spctl -a -vv --type install "dist/Voitta Bookmarklet-0.1.0.dmg"
... accepted
source=Notarized Developer ID
```

If you don't see `source=Notarized Developer ID` after a clean run,
**do not ship the DMG** — Gatekeeper will reject it on the recipient's
machine.

---

## 2. What works reliably

| Step | Tool | Notes |
| ---- | ---- | ----- |
| Frontend bundling | `vite` | Idempotent; cheap to re-run. |
| Stage resources into `src/voitta/resources/` | `cp -R` | Mirrors docs/, RAG scripts, seed scripts, plugins, bookmarklet source into the package data dir. Re-runs every build to pick up source edits without `--clean`. |
| Create scaffold | `briefcase create macOS app` | Downloads the CPython support package, installs deps into the bundle's standalone Python. Slow (~5 min) on first run, skipped on incremental builds. |
| Sync source | `briefcase update macOS app` | Cheap. Replaces `Contents/Resources/app/` with the current source tree. |
| Strip + ad-hoc sign | `briefcase build macOS app` | Always runs an ad-hoc pass first; later overwritten by `--identity` at package time. |
| Re-sign every Mach-O | `briefcase package macOS app --identity ...` | Walks every Mach-O in the bundle and signs with the Developer ID. **This part works.** It's the next part (DMG assembly) that breaks. |
| Apple notary submission | `xcrun notarytool submit ... --wait` | Reliable. Typical turnaround 2–15 min. Verdict appears as `status: Accepted`. |
| Staple ticket | `xcrun stapler staple` | Embeds the notary ticket so Gatekeeper passes the DMG offline (no network round-trip on first launch). |
| Gatekeeper verification | `spctl -a -vv --type install` | This is the same tool Gatekeeper uses internally. If it accepts the DMG, end-users will too. |

---

## 3. What's broken: briefcase's DMG packaging

### Symptom

`briefcase package` reports success, the script keeps going, Apple
notarises happily, the staple succeeds, `spctl` says `accepted`. The
DMG is exactly **~79 KB**. Mount it — it's empty (just an
`Applications` symlink, no `.app`).

Apple will notarise an empty DMG. Notarisation is a signature check,
not a content audit.

### Root cause

In the briefcase log, one line:

```
ditto: /Volumes/Voitta Bookmarklet 0.1.0/Voitta Bookmarklet.app: Operation not permitted
```

Briefcase mounts a fresh writable DMG with a volume name matching the
app version (`Voitta Bookmarklet 0.1.0`), then `ditto`s the `.app`
onto it. macOS **App Management privacy gate** refuses the copy when:

1. The destination is a mounted DMG volume,
2. The volume name resembles the app name being copied,
3. The copying process doesn't have a TCC grant for "App Management"
   or "Full Disk Access".

Whether (3) is satisfied depends on which application launched the
shell running `build_app.sh` — Terminal.app, iTerm, a CI agent, an
IDE-spawned shell, an MCP-spawned shell. Some inherit the grant; many
don't. Worse, briefcase ignores `ditto`'s non-zero exit and proceeds
to package and sign the empty volume.

### Detection

After `briefcase package` finishes, check the DMG size before
notarising:

```
DMG="dist/Voitta Bookmarklet-0.1.0.dmg"
if [ "$(stat -f %z "$DMG")" -lt 10000000 ]; then
    echo "DMG suspiciously small — assume briefcase swallowed the ditto failure"
fi
```

Anything below ~10 MB is wrong — the bundle itself is hundreds of
MB and even maximum-compression UDZO can't shrink it that far.

---

## 4. The workaround (manual repackage)

When the DMG is empty, the bundle in `build/voitta/macos/app/` is
fine — it's signed, hardened-runtime, and ready for distribution.
Only the DMG assembly broke. Repackage manually:

### 4a. Stage outside any mounted volume

```
rm -rf /tmp/dmg-stage
mkdir -p /tmp/dmg-stage
ditto "build/voitta/macos/app/Voitta Bookmarklet.app" \
      "/tmp/dmg-stage/Voitta Bookmarklet.app"
ln -s /Applications /tmp/dmg-stage/Applications
```

Writing to a regular folder isn't gated. Confirmed working.

### 4b. Create a writable staging DMG with a NEUTRAL volume name

```
hdiutil create -size 400m -fs HFS+ \
    -volname "AppStage" \
    -ov dist/staging.dmg
hdiutil attach -nobrowse dist/staging.dmg
```

**Critical:** the `-volname` must NOT resemble the app name. Names
that have triggered "Operation not permitted":

- `Voitta Bookmarklet 0.1.0` ✗
- `<App Name> X.Y.Z` (any version-suffixed app-name variant) ✗

Names that work:

- `AppStage` ✓
- `VoittaStage` ✓
- `dmg-stage` ✓

A short, generic, non-app-shaped name passes the App Management gate
because there's no name match to refuse.

### 4c. Copy in, then detach

```
ditto "/tmp/dmg-stage/Voitta Bookmarklet.app" \
      "/Volumes/AppStage/Voitta Bookmarklet.app"
ln -s /Applications "/Volumes/AppStage/Applications"
hdiutil detach "/Volumes/AppStage"
```

### 4d. Convert to compressed read-only DMG

```
rm -f "dist/Voitta Bookmarklet-0.1.0.dmg"
hdiutil convert dist/staging.dmg \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "dist/Voitta Bookmarklet-0.1.0.dmg"
rm -f dist/staging.dmg
```

UDZO + zlib-level=9 typically gets the 238 MB bundle down to ~60 MB.

### 4e. Re-sign, re-notarise, re-staple

```
codesign --sign "<DEV_IDENTITY>" --timestamp \
    "dist/Voitta Bookmarklet-0.1.0.dmg"

xcrun notarytool submit "dist/Voitta Bookmarklet-0.1.0.dmg" \
    --keychain-profile <NOTARY_PROFILE> \
    --wait

xcrun stapler staple "dist/Voitta Bookmarklet-0.1.0.dmg"

spctl -a -vv --type install "dist/Voitta Bookmarklet-0.1.0.dmg"
# expect: accepted / source=Notarized Developer ID
```

The second notary submission is unavoidable. The first one notarised
an empty DMG — that ticket is bound to the empty DMG's SHA and
doesn't transfer. Each notary submission costs nothing but a few
minutes of wall-clock time.

---

## 5. Things that LOOK like fixes but aren't

- **`xattr -cr` on the staged `.app`.** Does not remove
  `com.apple.provenance` (it's a system xattr; protected). And
  provenance is not the gate — the volume-name match is.
- **`cp -X` to skip xattrs during staging.** Same — macOS auto-applies
  provenance to copied app bundles regardless. Doesn't help.
- **Running with `sudo`.** Might work on some systems but breaks the
  signature chain (different uid creates files codesign later
  refuses to verify under your account). Not worth it.
- **`rsync --no-xattrs`.** macOS ships `openrsync` by default, which
  doesn't support `--no-xattrs`. You'd need Homebrew `rsync` — and
  even then, see above (xattrs aren't the cause).
- **`hdiutil create -srcfolder`.** Internally mounts a temp volume
  with `-volname` derived from the source folder name. Same
  Operation-not-permitted trap.

---

## 6. Suggested `build_app.sh` hardening

Two small guards would catch this every time. They've not been
added yet — apply them if you build this DMG more than once a week.

**Guard 1** — verify DMG size before notarising:

```
DMG=$(ls -1 "$ROOT/dist/"*.dmg 2>/dev/null | head -1)
if [ -n "$DMG" ] && [ "$(stat -f %z "$DMG")" -lt 10000000 ]; then
    echo "[build_app] DMG is suspiciously small — briefcase likely tripped App Management gate" >&2
    exit 1
fi
```

**Guard 2** — repackage automatically if briefcase emitted the
known-bad DMG:

```
if [ "$(stat -f %z "$DMG")" -lt 10000000 ]; then
    echo "[build_app] repackaging DMG manually..."
    # (4a–4d above)
fi
```

Guard 1 is the simpler ship — it fails loud so a human applies the
workaround. Guard 2 hides the bug, which is also fine, but means
future-you won't notice when briefcase or macOS fixes the underlying
issue.

---

## 7. Recipient experience (when the build is correct)

1. Recipient downloads the DMG.
2. Double-clicks. Finder mounts it; no Gatekeeper prompt because
   the DMG is signed AND stapled.
3. Drags `Voitta Bookmarklet.app` to the `Applications` symlink.
4. Launches from `/Applications`. Stapled ticket means no
   `"<App> is from the internet"` warning, no `xattr -d
   com.apple.quarantine` ritual. First launch goes straight to the
   menu bar.

If any step asks for an override or shows a yellow warning, the
build was *not* clean — check that `spctl -a -vv --type install`
reports `source=Notarized Developer ID`, not `Notarized` alone or
`Developer ID` alone. The wording is what differs.

---

## 8. One-time prerequisites

These are not part of the build script — set up once per machine.

```
# Apple Developer cert imported to login keychain.
# Verify with:
security find-identity -v -p codesigning
# expect at least: "Developer ID Application: <Your Name> (<TEAM_ID>)"

# Notary profile (stored in the login keychain).
xcrun notarytool store-credentials <NOTARY_PROFILE> \
    --apple-id <your-apple-id@example.com> \
    --team-id <TEAM_ID>
# Paste an app-specific password generated at
# https://appleid.apple.com → Sign-In and Security.
```

Verify a notary profile exists:

```
xcrun notarytool history --keychain-profile <NOTARY_PROFILE> | head
```

---

## 9. References

- briefcase docs: <https://briefcase.readthedocs.io/>
- Apple notarytool: `man notarytool`, `xcrun notarytool --help`
- Apple App Management gate (the cause of the briefcase DMG bug):
  <https://developer.apple.com/documentation/security/app_management>
- Gatekeeper recipient flow: <https://support.apple.com/guide/security/gatekeeper-and-runtime-protection-sec5599b66df/web>
