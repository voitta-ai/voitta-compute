# Serverless (In-Browser) Report Generation — Proposal

## 1. Context

Today's report-generation venue is **server-side**:

> LLM generates Python → Python pulls data → Python renders the report.

This proposal explores a **100% serverless** alternative:

> LLM generates JavaScript → JavaScript pulls data → JavaScript renders the report,
> entirely in the browser, with no backend.

The goal of this document is **not** to argue for or against — it is to enumerate
exactly what must be built to make the serverless path real, and to sketch a
rough implementation approach for each item.

**Verdict up front:** rendering in-browser is easy. The hard parts are the
**compute layer**, **safe execution of generated code**, **data at scale**, the
**LLM orchestration loop**, and — the genuine blocker — **secret management**.
A truly keyless-server design cannot keep an API key secret. Every other item
is "hard but buildable"; that one needs *some* trusted endpoint or a different
auth model (see §3.1).

---

## 2. Architecture sketch

```
┌─────────────────────────────────────────────────────────────┐
│ Browser (single tab)                                          │
│                                                               │
│  ┌────────────┐   prompt/context   ┌──────────────────────┐  │
│  │ Orchestrator│ ─────────────────▶ │  LLM API (direct)    │  │
│  │  (JS loop)  │ ◀───────────────── │                      │  │
│  └─────┬──────┘   generated JS      └──────────────────────┘  │
│        │                                                       │
│        │ eval in sandbox                                       │
│        ▼                                                       │
│  ┌──────────────────────────┐    pulls data   ┌────────────┐  │
│  │ Sandboxed iframe / Worker │ ───────────────▶│ Data source│  │
│  │  - compute (df-equiv)     │ ◀───────────────│  (CORS+    │  │
│  │  - render (DOM/SVG/canvas)│    rows/JSON     │   auth)    │  │
│  └─────────────┬─────────────┘                 └────────────┘  │
│                │ HTML/PDF                                       │
│                ▼                                                │
│           Report artifact                                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Items to implement

### 3.1 Secret management *(the blocker — solve first)*

**Problem.** With no backend, the LLM API key and any data credentials would
live in client-side code, readable by anyone. There is no proxy to hide them.

**Rough approach (pick one):**
- **A — User-supplied key.** The user pastes their own LLM API key; it stays in
  `sessionStorage` (never persisted, never shipped). Acceptable for internal /
  power-user tools, not for a product. *Lowest effort, honest about the model.*
- **B — Per-user short-lived token.** A *minimal* auth endpoint (the one piece
  that isn't serverless) mints scoped, short-TTL tokens for the LLM + data APIs.
  This is the "hybrid" that's still 95% browser. Recommended if any non-technical
  user touches it.
- **C — Provider-native browser auth.** Use an LLM/data provider that supports
  OAuth/PKCE or signed ephemeral credentials issued to the browser directly.
  Eliminates the static key but constrains provider choice.

**Decision required from stakeholders before anything else is built.**

---

### 3.2 LLM orchestration loop (in JS)

**Problem.** Today Python owns the generate→execute→observe→retry loop, context
assembly, token-budget management, tool-call dispatch, and the "report done?"
decision. All of this must move into the browser.

**Rough approach:**
- Port the agent loop to TypeScript: a state machine `(plan → generate code →
  execute → capture result/error → feed back → repeat → finalize)`.
- Maintain conversation history + token accounting in memory; trim/summarize
  older turns to stay within the context window.
- Tool/function calls dispatched to local JS handlers (data fetch, compute,
  render) instead of Python callables.
- Stream responses for responsiveness; persist transcript to `IndexedDB` so a
  reload can resume.

---

### 3.3 Safe execution of generated code

**Problem.** Generated JS runs in the user's session (cookies, tokens, DOM).
Naive `eval` is a self-built XSS engine.

**Rough approach:**
- Execute generated code inside a **sandboxed `<iframe sandbox>`** or a **Web
  Worker** with **no DOM and no ambient credentials**.
- Communicate only via `postMessage` with a narrow, typed API surface
  (request data, return computed rows / render instructions).
- Enforce a strict **Content-Security-Policy**; deny network except an
  allow-listed data origin; no access to `window.parent` secrets.
- Wrap execution in timeouts + memory caps; treat the sandbox as hostile.

---

### 3.4 Compute layer (pandas / numpy equivalent)

**Problem.** Browser JS lacks a mature dataframe/stats stack. Aggregations,
joins, group-bys, and basic ML are painful with raw JS.

**Rough approach:**
- Adopt **Arquero** or **DuckDB-WASM** as the in-browser dataframe/SQL engine.
  DuckDB-WASM is the strong option: real SQL, columnar, handles joins/aggregates
  on hundreds of MB, reads Parquet/CSV/Arrow directly.
- Expose a small, stable API to the generated code (`query(sql)`, `df.groupby…`)
  so the LLM targets a known surface, not arbitrary libraries.
- Keep the engine instance inside the sandbox (§3.3).

---

### 3.5 Data access at scale

**Problem.** Everything streams over the network into one tab's RAM. CORS,
memory limits, pagination, no native DB drivers.

**Rough approach:**
- Require data sources to expose **CORS-enabled, paginated/streamed** endpoints
  (ideally **Arrow/Parquet over HTTP** so DuckDB-WASM can range-request).
- Stream + chunk; never load a full large table into JS arrays.
- Cache pulled partitions in `IndexedDB` / OPFS for re-runs.
- Set explicit size ceilings; above them, fall back to the server path.

---

### 3.6 Rendering

**Problem.** (Smallest item — browser is good at this.)

**Rough approach:**
- Render to semantic HTML + CSS; charts via a JS lib (Vega-Lite / Observable
  Plot / ECharts) driven by the generated code's output spec.
- Keep render instructions **declarative** (a spec the sandbox returns) rather
  than letting generated code touch the host DOM directly.

---

### 3.7 Export & persistence

**Problem.** Serverless has no place to store or reproduce artifacts;
print-to-PDF is low quality.

**Rough approach:**
- Client PDF via headless print API or a WASM renderer; XLSX via SheetJS.
- Persist report spec + data snapshot to `IndexedDB`/OPFS so it's reproducible
  locally; offer "download bundle" for sharing.
- Accept that durable/shareable storage implies *some* remote sink (ties back
  to the hybrid in §3.1-B).

---

### 3.8 Observability, cost & rate-limit guardrails

**Problem.** No server means no logging, no rate limiting, no LLM cost cap, no
audit trail — all of which the server path gives for free.

**Rough approach:**
- Client-side budget meter: token/$ counter that hard-stops the loop.
- Local structured logging to `IndexedDB`; optional best-effort telemetry beacon.
- Rate-limit in the orchestrator; honor provider 429s with backoff.
- Note: real audit/compliance logging effectively requires a trusted endpoint.

---

## 4. Effort & risk summary

| Item | Difficulty | Risk | Notes |
|------|-----------|------|-------|
| 3.1 Secrets | — | **Blocker** | Needs a model decision; pure-serverless can't hide a static key |
| 3.2 Orchestration loop | High | Med | Re-port of existing Python agent logic |
| 3.3 Code sandbox | High | **High** | This is your security boundary now |
| 3.4 Compute layer | Med | Low | DuckDB-WASM de-risks most of it |
| 3.5 Data at scale | Med | Med | Depends on data sources exposing CORS/Arrow |
| 3.6 Rendering | Low | Low | Browser's home turf |
| 3.7 Export/persistence | Med | Low | Quality + durability caveats |
| 3.8 Observability | Med | Med | Largely lost without an endpoint |

---

## 5. Recommendation

- **Pure 100% serverless** is viable only for **light/medium reports on
  already-shaped, small-ish, CORS-accessible data**, and only with the
  **user-supplied-key** secret model (§3.1-A).
- For anything broader, the pragmatic target is the **thin-server hybrid**:
  browser does compute + execution + render; a minimal endpoint mints
  short-lived tokens (§3.1-B) and optionally sinks artifacts/audit logs.
  This keeps ~95% of the work client-side while solving the one truly
  unsolvable serverless gap.
- Suggested sequencing: **decide §3.1 → prototype §3.3 + §3.4 together (sandbox
  running DuckDB-WASM) → port §3.2 → wire §3.5 → polish §3.6/§3.7 → add §3.8.**

---

*Draft proposal — no code implemented. For discussion.*
