# Drift Notes — docs vs actuality

Started 2026-05-08 on branch `dogfood-bookmarklet-ai` while dogfooding the
bookmarklet on drive.google.com per cofounder instructions.

## Drift items

### D1. Bookmarklet URL: hosted vs local
- **Cofounder instruction**: bookmarklet points to `https://bookmarklet.voitta.ai`
  (loads `/widget.js` from there).
- **Repo docs (`README.md` step 4, `bookmarklet/bookmarklet.js`)**: bookmarklet
  points to `https://127.0.0.1:12358` (local backend).
- **Live state of `bookmarklet.voitta.ai` at 2026-05-08 ~05:08 UTC**:
  HTTP/1.1 502 Bad Gateway (nginx up, upstream down) for `/`, `/widget.js`,
  `/health`.
- **No reference to `bookmarklet.voitta.ai` anywhere in the repo** (`grep -rn` clean).
- **Implication**: cofounder's hosted deploy is a parallel artifact not yet
  reflected in this repo. README still describes the original local-backend
  flow.

### D2. README has no mention of "browser-side compute / wasm"
- **Cofounder context**: "computation model that does not require too much
  compute on our end. Use browser to compute (web assembly and shit). It works."
- **Repo docs**: backend is FastAPI on `127.0.0.1:12358`; LLM adapters live
  server-side (`backend/app/services/`). No wasm in `frontend/` (to verify
  during this session — current grep finds none in code paths I checked).
- **Implication**: either (a) the wasm shift is in flight on a not-yet-committed
  branch, or (b) it lives on the hosted-widget side, or (c) the comment refers
  to future architecture. Confirm with cofounder before reconciling docs.

## Actions taken this session

- Created worktree
  `voitta-bookmarklet.worktrees/dogfood-bookmarklet-ai` on branch
  `dogfood-bookmarklet-ai` from `master @ 2d6aa27`.

### D3. Bookmarklet has stale `http://` variants in the wild
- During dogfooding session the user encountered an older bookmark with
  `B = "http://127.0.0.1:12358"`. README is `https://`. The HTTP variant
  produces a confusing `s.onerror` alert "could not load widget from
  http://..." even when the backend is running, because run.sh detects the
  mkcert pair and only binds HTTPS.
- **Implication**: `--http` flag is supported but README doesn't warn that
  upgrading from a pre-cert run leaves stale HTTP bookmarks behind. Users will
  re-paste from README when in doubt.

### D4. UX on Drive: silent load, handle hard to find
- Widget loads on `drive.google.com` and bridge handshakes successfully, but
  default state is closed. Only UI cue is a 22×60 px dark tab at right edge
  vertical center. Drive's own rail visually consumes that area.
- Tracked as https://github.com/voitta-ai/voitta-bookmarklet/issues/1.

### D5. Bookmarklet fails entirely on github.com (CSP)
- github.com repo READMEs serve a strict `Content-Security-Policy` that
  blocks `script-src https://127.0.0.1:12358`. Bookmarklet's `s.onerror`
  fires → alert → user sees the same "is the backend running?" message that
  also fires for backend-down / wrong-scheme / mkcert-untrusted. The error
  message is too generic given how many causes share it.
- Not yet filed as a separate issue; recorded here for reconciliation.

## Open questions for reconciliation

1. Should README describe the hosted bookmarklet flow as the default and the
   local-backend flow as a contributor path, or vice-versa?
2. Is `bookmarklet.voitta.ai` infra in this repo's `scripts/` or in a separate
   deploy repo? (Couldn't find it here.)
3. Is the wasm/browser-compute work merged anywhere yet, or tracking elsewhere?
4. Should `s.onerror` alert distinguish the four failure modes (backend down,
   wrong scheme, CSP block, untrusted cert)?
