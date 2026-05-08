# Handoff: Voitta bookmarklet dogfooding

**Created:** 2026-05-08
**Author:** Claude Code session (with Greg)
**For:** AI agent (next session) + self
**Status:** Ready to execute — drift recorded, one issue filed, backend stopped

---

## Summary

Dogfooded the Voitta bookmarklet end-to-end against `drive.google.com` per the
cofounder's instructions. The hosted bookmarklet variant
(`https://bookmarklet.voitta.ai`) was 502'd at session time, so we set up the
local backend, brought up the widget, and validated the flow. Discovered five
classes of drift between docs/cofounder-bookmarklet/repo. All recorded in
`DRIFT_NOTES.md`. One UX issue filed: voitta-bookmarklet#1.

## Project Context

- **Repo:** `voitta-ai/voitta-bookmarklet` (origin = ssh).
- **Worktree:** `~/g/git.voitta/voitta-bookmarklet.worktrees/dogfood-bookmarklet-ai`
  on branch `dogfood-bookmarklet-ai` from `master @ 2d6aa27`.
- **Stack:** FastAPI backend (Python 3.14, uvicorn, HTTPS via mkcert) on
  `127.0.0.1:12358`; Preact widget bundled by Vite into a single
  `dist/widget.js` IIFE; bookmarklet injects `<script src=".../widget.js">`
  into any HTTPS page.
- **What it does:** drawer-style chat pane in Shadow DOM on host page, can
  call provider tools (Drive read-only today) on the user's behalf.

## The Plan

The session was driven directly by the cofounder's instruction (no
`.claude/plans/` file). The instruction was:

> create a bookmark with [bookmarklet code]. Open Bookmarks Manager → 3 dots →
> paste as URL. Go to drive.google.com. Click bookmark. Once key set up, ask
> something like "generate me a shit report of sorts." A computation model
> that doesn't require too much compute on our end — browser does the work.

Cofounder bookmarklet pointed to `https://bookmarklet.voitta.ai`, which was
502 at session time. We pivoted to the local backend.

## Key Files

| File | Why It Matters |
|------|---------------|
| `DRIFT_NOTES.md` | All five drift items (D1-D5) and four reconciliation questions. Read this first. |
| `README.md` | Step 4 has the canonical local bookmarklet URL (HTTPS). Untouched this session. |
| `bookmarklet/bookmarklet.js` | Readable source of the local bookmarklet. Diverges from cofounder's hosted variant. |
| `frontend/src/components/ChatPane.tsx:48` | `loadInitialOpen()` — defaults closed; root cause of "nothing happens on Drive". |
| `frontend/src/styles.css:20-40` | `.handle` (22×60 px right-edge tab) — the only visible UI when widget is closed. |
| `backend/run.sh` | mkcert provisioning + uvicorn launch. Auto-detects cert pair to decide HTTPS vs HTTP. |

## Current State

**Done:**
- Worktree + branch created (`dogfood-bookmarklet-ai`).
- Backend dependencies installed in `backend/.venv` (had to run
  `/Applications/Python\ 3.14/Install\ Certificates.command` first; the
  python.org Python 3.14 install ships without a populated cert bundle and
  pip's PyStemmer build fetched from a TLS source that failed verify).
- mkcert installed via `brew install mkcert nss`, CA installed via
  `mkcert -install` (required interactive sudo).
- TLS cert pair generated at `backend/certs/127.0.0.1+1{,-key}.pem`.
- Frontend built (`npm run build` → `dist/widget.js`, 3.4 MB).
- RAG index built (103 chunks, 13 files).
- Backend started, validated `https://127.0.0.1:12358/health` returns ok.
- Bookmarklet validated end-to-end on `drive.google.com`: bridge handshake
  succeeded, widget mounted in Shadow DOM, handle visible at right edge.
- Issue filed: https://github.com/voitta-ai/voitta-bookmarklet/issues/1

**In Progress:**
- None.

**Not Started:**
- Reconciliation of D1-D5 in `DRIFT_NOTES.md`. Most items need cofounder
  input (e.g., "is `bookmarklet.voitta.ai` infra in this repo or elsewhere?").
- Possible README update once D1/D2 are reconciled.
- The actual "shit report" smoke test — user saw the handle, opened the
  drawer, confirmed "I see it"; we did not run an end-to-end LLM chat with
  an API key in this session.

## Decisions Made

- **Used local backend instead of waiting for `bookmarklet.voitta.ai`** — the
  hosted upstream was 502 at session time. User decision: "Let's see if we
  can start our backend, start keeping notes on the drift between
  actuality/docs. We will reconcile later."
- **Did not edit README this session** — README's local-flow description is
  still accurate for local testing. Drift around the hosted URL is recorded
  in `DRIFT_NOTES.md` rather than re-written into README without cofounder
  input.
- **Filed only the Drive UX issue, not the GitHub CSP failure** — the CSP
  block on github.com is documented in `DRIFT_NOTES.md` D5 but not yet a
  GitHub issue. Decision was scope discipline: one issue per session unless
  cofounder asks otherwise.
- **`brew install mkcert nss` (not just mkcert)** — `nss` ships
  `certutil`/`pk12util` so mkcert can write its CA into the Firefox/Chromium
  NSS DB too, not just the macOS Keychain.

## Important Context

- **Python 3.14 SSL cert install gotcha:** if you `python3 -m venv .venv`
  with the python.org Python 3.14 build, pip install of any package whose
  build script does an HTTPS fetch (PyStemmer is the canary here) fails with
  `[SSL: CERTIFICATE_VERIFY_FAILED]`. Fix: run
  `"/Applications/Python 3.14/Install Certificates.command"` once, then
  recreate the venv. This is not Voitta-specific.
- **Backend HTTPS-only when certs present:** `run.sh` autoselects HTTPS if
  both `backend/certs/*.pem` files exist. Old `B="http://..."` bookmarklets
  silently fail against an HTTPS-only backend with the generic
  "could not load widget" alert. Pre-cert bookmarks linger in users' bars.
- **The generic `s.onerror` alert hides four distinct failure modes:**
  backend down, wrong scheme, CSP block, untrusted cert. Recorded as
  open question 4 in `DRIFT_NOTES.md`.
- **GitHub.com is a hostile host page** for the bookmarklet (CSP
  `script-src` blocks 127.0.0.1). drive.google.com works.
- **Default `open = false` on first mount** is the root cause of
  the "nothing happens on Drive" complaint. The handle exists but is
  swallowed by Drive's right rail.
- **Backend is not running at session end.** User asked it killed:
  `kill $(lsof -t -iTCP:12358 -sTCP:LISTEN)` was run, port 12358 confirmed
  free.
- **Memory written:** `feedback_screenshot_location.md` (screenshots in
  `~/Downloads`, not `~/Desktop`) and
  `project_voitta_bookmarklet_gdrive_handle.md` (Drive silent-load gotcha).

## Next Steps

1. **Reconcile D1 (bookmarklet.voitta.ai vs local):** ask cofounder where
   `bookmarklet.voitta.ai` is deployed from. If it's a forked repo or a
   private deploy infra, point this repo's docs at it. If it's a future
   deploy of *this* repo's `frontend/dist/widget.js`, add a `scripts/deploy/`
   target.
2. **Reconcile D2 (wasm/browser-compute claim):** confirm with cofounder
   whether the wasm compute path is in flight on a branch we don't yet see,
   or whether the comment was forward-looking. If in flight, branch off or
   pull when ready.
3. **Address voitta-bookmarklet#1:** smallest-impact fix is probably to
   default `open = true` when no `STORAGE_OPEN_KEY` is in `sessionStorage`
   (keep persistence for repeat visits). One-line change in
   `frontend/src/components/ChatPane.tsx:48-54`.
4. **Decide on the github.com CSP failure (D5):** either file it as a
   separate issue with a "wontfix — known CSP-strict host" label, or
   improve the alert message to mention CSP as a likely cause when the load
   fails on a CSP-strict origin.
5. **Run the actual LLM chat smoke test** — Greg has only verified the
   handle was visible, not the round trip with an API key. Set a key in the
   widget Settings (⚙) and test the "shit report" prompt.

## Constraints

- **Never edit README to point at `bookmarklet.voitta.ai` until D1 is
  reconciled.** The hosted URL was 502 during this session. Don't enshrine
  uncertainty in README.
- **Never commit secrets.** Settings live at
  `~/.config/voitta-bookmarklet/settings.json` (0600); no API keys in repo.
- **Worktrees pattern:** all branch work in
  `~/g/git.voitta/voitta-bookmarklet.worktrees/<branch>/`, not in
  `voitta-bookmarklet/` directly.
- **Don't mock the backend in any tests added.** (Inferred from repo
  practice — `backend/tests/` exercises the real registry / providers.)
- **Local Python 3.14 + python.org install needs cert init before any new
  venv.** Don't skip the Install Certificates step.
