# Desktop app — packaging, signing, notarisation

The macOS menu-bar app is built by [briefcase](https://briefcase.readthedocs.io/) from [pyproject.toml](../pyproject.toml) at the repo root. End-user artefacts (`.app`, `.dmg`) land in `build/` and `dist/`.

## TL;DR

```bash
./build_app.sh                                        # build only (~5 min, 240 MB)
./build_app.sh --package                              # + DMG (ad-hoc signed)
./build_app.sh --package --sign "<identity>" --notarize   # signed + notarised
```

The third form is what you ship. The others are for development.

---

## Distribution friction tiers

| Mode | Cost | Recipient experience |
| ---- | ---- | ---- |
| Ad-hoc | free | Gatekeeper blocks first launch; right-click → Open required. |
| Developer ID, no notarisation | $99/yr | "Identified developer" wording but Gatekeeper still blocks first launch on macOS 10.15+. **Don't bother — no friction win over ad-hoc.** |
| Developer ID + notarised + stapled | $99/yr | Double-click, no warning, no friction. |

The $99/yr is the Apple Developer Program — same fee for individual or organisation. Notarisation submissions themselves are free and unlimited.

---

## One-time setup (after paying for membership)

### 1. Get the Developer ID Application certificate

1. Sign in at [developer.apple.com](https://developer.apple.com).
2. Account → Certificates, Identifiers & Profiles → Certificates → "+" → **Developer ID Application**.
3. Follow the CSR walk-through (Keychain Access → Certificate Assistant → Request a Certificate from a Certificate Authority …). Save the `.certSigningRequest` to disk, upload it to the developer portal.
4. Download the resulting `developerID_application.cer`, double-click → installs into your login keychain.

Verify the identity is visible to `codesign`:

```bash
security find-identity -p codesigning -v
# 1) <hex hash>  "Developer ID Application: <Your Name> (ABCDEF1234)"
```

The **(ABCDEF1234)** suffix is your team ID; remember it.

### 2. Generate an app-specific password

Apple ID passwords don't work directly with `notarytool`. Generate an app-specific password:

1. [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords → "+" .
2. Label it "Voitta notarytool" or similar. Apple shows the password **once** — copy it now.

### 3. Stash the password in your keychain (one time)

```bash
xcrun notarytool store-credentials voitta-notary \
  --apple-id you@example.com --team-id ABCDEF1234
# When prompted, paste the app-specific password from step 2.
```

This creates a keychain item named **voitta-notary** that `xcrun notarytool` will look up by name. You never type the password into a script. The build script defaults to `voitta-notary`; override via `VOITTA_NOTARY_PROFILE=other-profile`.

---

## Producing a signed + notarised release

```bash
./build_app.sh --clean --package \
  --sign "Developer ID Application: <Your Name> (ABCDEF1234)" \
  --notarize
```

What happens:

1. Briefcase signs every Mach-O file in the bundle (~80 dylibs/.so files including the bundled Python.framework) with the given identity, then signs the outer `.app`.
2. Briefcase packages a DMG.
3. The script submits the DMG to Apple's notary service via `xcrun notarytool submit … --wait`. Typical turnaround 2-15 min; the `--wait` flag blocks until Apple returns Accepted or Invalid.
4. On success, `xcrun stapler staple` embeds the notarisation ticket directly in the DMG so it passes Gatekeeper offline.
5. `spctl -a -vv --type install` verifies the staple worked end-to-end.

Output: `dist/Voitta Bookmarklet-0.1.0.dmg`. Drop it on Slack / Dropbox / a download page; recipient downloads, double-clicks, drags to /Applications, double-clicks Voitta — no warning, no friction.

### What if notarisation fails?

The script prints the submission ID and the exact command to fetch the log:

```bash
xcrun notarytool log <submission-id> --keychain-profile voitta-notary
```

The log usually points at one of:

- A missing entitlement (we wired the four common Python-app entitlements into [pyproject.toml](../pyproject.toml); should be a non-issue).
- An unsigned dylib briefcase missed (rare; resign manually with the identity and resubmit).
- A denylisted symbol (e.g. an old library calling `gettimeofday` via a private framework); fix by upgrading the offending wheel.

---

## Hardened runtime entitlements

Bundled into [pyproject.toml](../pyproject.toml). All four are unavoidable for a Python app shipping its own interpreter + a long tail of C extensions:

| Entitlement | Why we need it |
| ---- | ---- |
| `com.apple.security.cs.allow-jit` | Python's bytecode interpreter mprotects RWX pages; same for ctypes callback trampolines. |
| `com.apple.security.cs.allow-unsigned-executable-memory` | Broader umbrella catching the same pattern in C extensions. Belt-and-braces with `allow-jit`. |
| `com.apple.security.cs.disable-library-validation` | We vendor ~80 C extensions (h5py, scipy, pandas, panel/bokeh, chromadb's rust bindings, …) signed by their wheel builders, not by us. Default library validation refuses to load any dylib whose signature isn't Apple's or our team's. |
| `com.apple.security.cs.allow-dyld-environment-variables` | Briefcase's launcher uses `DYLD_FRAMEWORK_PATH` to point at the bundled Python.framework. |

These have **no effect on ad-hoc builds** — `codesign` embeds them, but macOS only enforces them on signed bundles launched through Gatekeeper.

---

## Auto-update (future work)

Sparkle is the canonical macOS auto-update framework. Once we have a stable cert + a hosting URL for the appcast XML, [briefcase-sparkle](https://github.com/beeware/briefcase) (planned) or a manual integration can wire it in. Not a priority until we ship to more than a handful of users.

---

## Why not Mac App Store?

Three independent hard blockers:

1. **`exec(compile(user_code))`** in `run_compute` violates [App Store Review Guideline 2.5.2](https://developer.apple.com/app-store/review/guidelines/) — "execute code which introduces or changes features".
2. **Bookmarklet injects JS into arbitrary 3rd-party sites** — Guideline 5.2, "modifying third-party functionality".
3. **`mkcert` writes a root CA** — sandbox-prohibited.

Notarised DMG is the right delivery channel; App Store would gut the product. See [conversation history](./README.md) — earlier sessions explored this.
