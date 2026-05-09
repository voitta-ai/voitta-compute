# Voitta Bookmarklet

A right-side chat pane you can inject into any web page with a
one-click bookmark. Backend is FastAPI on `https://127.0.0.1:12358`;
chat runs against your choice of **Anthropic**, **OpenAI**, or
**Gemini**; data-provider tools (Google Drive today, more on the
roadmap) reach into third-party services on the user's behalf.

## Project layout

```
voitta-bookmarklet/
├── README.md                 ← you are here
├── docs/                     ← agent's authoritative reference (RAG-indexed)
│   ├── 00-overview.md
│   ├── 01-architecture.md
│   ├── 02-frontend.md
│   ├── 03-providers.md
│   ├── 04-tool-catalog.md
│   ├── 05-bridge-protocol.md
│   ├── 07-report-scripts.md
│   └── 08-drive-tools.md
├── backend/
│   ├── pyproject.toml
│   ├── run.sh
│   ├── certs/                ← mkcert cert+key (gitignored)
│   ├── tests/                ← pytest: registry, providers, drive context, config
│   └── app/
│       ├── main.py
│       ├── config.py         ← server constants + the Voitta system prompt
│       ├── routes/
│       ├── services/         ← LLM adapters, OAuth, Panel renderer, scripts, …
│       ├── bridge/           ← in-memory ToolBridge (sessions/queues/futures)
│       └── tools/
│           ├── registry.py
│           ├── browser.py
│           ├── rag/
│           ├── domain/       ← provider-agnostic tools (rag, web, screenshot, …)
│           └── providers/    ← provider-specific tools (drive today, more later)
│               └── drive/
├── frontend/
│   ├── package.json
│   ├── vite.config.ts        ← single IIFE → dist/widget.js
│   ├── vitest.config.ts
│   ├── index.html            ← dev harness
│   └── src/
│       ├── main.tsx
│       ├── widget.tsx        ← Shadow DOM host + Preact mount
│       ├── theme.css         ← design tokens — swap to rebrand
│       ├── styles.css        ← consumes theme tokens
│       ├── lib/              ← bridge, primitives, buffers, settings, …
│       └── components/
├── bookmarklet/
│   └── bookmarklet.js        ← readable source for the URL
├── libs-info/
│   └── panel/                ← HoloViz Panel source (RAG-indexed for report authoring)
├── rag/
│   ├── build_rag.py          ← indexes docs/
│   └── build_panel_rag.py    ← indexes libs-info/panel/
├── python_storage/           ← snapshot cache (gitignored)
└── scripts/
    ├── compute/              ← user-defined compute scripts (gitignored)
    └── reports/              ← user-defined report scripts (gitignored)
```

## First-time setup

### 0. TLS cert for the chat backend (one-time)

```bash
brew install mkcert
sudo mkcert -install
mkdir -p backend/certs && cd backend/certs
mkcert 127.0.0.1 localhost
cd ../..
```

`backend/certs/` is gitignored. The cert auto-detects on backend start.

### 1. Start the backend

```bash
cd backend
./run.sh
```

`run.sh` creates a `.venv`, installs dependencies (`pip install -e .`),
and starts uvicorn on `127.0.0.1:12358`. Sanity check:

```bash
CACERT="$HOME/Library/Application Support/mkcert/rootCA.pem"
curl --cacert "$CACERT" https://127.0.0.1:12358/health
# {"ok":true,"providers_supported":["anthropic","openai","gemini"], ...}
```

### 2. Build the RAG indexes

```bash
backend/.venv/bin/python rag/build_rag.py
backend/.venv/bin/python rag/build_panel_rag.py   # optional, large
```

Re-run when you change `docs/`. The chat backend lazy-loads on first
`rag_query`, no restart needed.

### 3. Build the frontend widget

```bash
cd frontend
npm install
npm run build      # produces dist/widget.js
```

Re-run on every frontend change. (Or `npm run dev` for the standalone
Vite harness at `http://localhost:5173`.)

### 4. Browser CSP extension (recommended)

Strict-CSP sites (eBay, GitHub, some banks) refuse to load scripts from
`127.0.0.1:12358` by default. Install one Chrome extension to bypass:

* **CSP Unblock** (4.5★) — universal, strips all CSP headers everywhere.
* **Anti-CORS, anti-CSP** (4.3★) — per-hostname allowlist, narrower
  blast radius.

Either is safe to leave on the sites you actually use — turn it off
elsewhere. Without it, the bookmarklet will still load on most sites,
but eBay-class CSP-strict pages will silently refuse to fetch
`/widget.js`.

If you're not sure whether you need it: try the bookmarklet on a site,
open DevTools → Console. If you see *"Loading the script
https://127.0.0.1:12358/widget.js... violates the following Content
Security Policy directive ... The action has been blocked"*, you need
the extension. *"...The policy is report-only, so the violation has
been logged but no further action has been taken"* — extension
optional, the bookmarklet still works.

### 5. Use the bookmarklet

The minified one-liner:

```
javascript:(function(){var B="https://127.0.0.1:12358";if(window.__voittaBookmarkletLoaded){if(window.VoittaBookmarklet&&typeof window.VoittaBookmarklet.mount==="function")window.VoittaBookmarklet.mount();return;}window.__voittaBookmarkletLoaded=true;var s=document.createElement("script");s.src=B+"/widget.js?t="+Date.now();s.async=true;s.onerror=function(){window.__voittaBookmarkletLoaded=false;alert("Voitta bookmarklet: could not load widget from "+B+"\n\nIs the backend running? (cd backend && ./run.sh)");};document.documentElement.appendChild(s);})();
```

Add as a bookmark and click on any HTTPS page. The backend must be
running. Readable source:
[`bookmarklet/bookmarklet.js`](bookmarklet/bookmarklet.js).

## Configuration

Server-side constants live in
[`backend/app/config.py`](backend/app/config.py) (no env vars, no
`.env`):

| Constant | Default | Notes |
| -------- | ------- | ----- |
| `HOST` | `127.0.0.1` | uvicorn bind host |
| `PORT` | `12358` | uvicorn port |
| `TLS_CERT_PATH` / `TLS_KEY_PATH` | auto-detected from `backend/certs/` | HTTPS iff both files exist |
| `MAX_TOKENS` | `16384` | Default per-iteration response cap |
| `MAX_TOOL_ITERATIONS` | `25` | Default tool-use loop cap |
| `MAX_TOOL_ITERATIONS_CEILING` | `200` | Hard server-side ceiling |

LLM keys live in the **Settings panel** in the drawer (⚙ button) and
persist on the local backend at
`~/.config/voitta-bookmarklet/settings.json` (`0600`).

### Connecting Google Drive

Drive tools appear in the LLM's tool list once OAuth is configured:

1. Create a Google Cloud OAuth client (Desktop or Web type, redirect
   URI `https://127.0.0.1:12358/api/google/oauth/callback`).
2. Paste `googleOAuth.clientId` and `googleOAuth.clientSecret` into
   `~/.config/voitta-bookmarklet/settings.json`.
3. In the in-pane Settings, click **Connect Google Drive**.

Scope is `drive.readonly`. Read-only by design — there are no upload
or share tools.

## Tests

Backend (`pytest`):

```bash
cd backend
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

Frontend (`vitest`):

```bash
cd frontend
npm test
```

## Theming

The visual tokens (colours, typography, shape) live in
[`frontend/src/theme.css`](frontend/src/theme.css). `styles.css`
consumes them via `var(--voitta-…)`. To rebrand, swap the values in
`theme.css` and rebuild.

## Adding a data provider

Every data provider lives in its own subpackage under
`backend/app/tools/providers/`. The recipe is in
[docs/04-tool-catalog.md](docs/04-tool-catalog.md). Short version:

1. `backend/app/tools/providers/<name>/context.py` — host-gated
   `<name>_get_page_context` tool.
2. `backend/app/tools/providers/<name>/tools.py` — action tools,
   visibility-gated by an OAuth-status check.
3. Mirror `app/services/google_oauth.py` if the provider needs OAuth.
4. Wire the new package into `app/tools/providers/__init__.py`.

## Where to read next

- New here? [docs/00-overview.md](docs/00-overview.md).
- Wiring a new tool? [docs/04-tool-catalog.md](docs/04-tool-catalog.md).
- Debugging the bridge? [docs/05-bridge-protocol.md](docs/05-bridge-protocol.md).
- Switching LLM providers? [docs/03-providers.md](docs/03-providers.md).
