# Reports

A "report" is a user-authored Python script that produces visual
content mountable in the widget. Scripts live at
`scripts/<name>/code.py` and must define `build(ctx)`.

**There is one report kind: HTML.** `build()` returns a string —
the body of an HTML document. The system caches it, mounts it as
an iframe, and injects a screenshot shim into `<head>`. That's it.

Anything you want — Plotly charts, ELK diagrams, three.js scenes,
matplotlib PNGs, KPI cards, interactive widgets — you embed
inside the HTML directly. Every JS library you want runs in the
iframe via a `<script src="…cdn…">` tag. Every Python-rendered
image (matplotlib, etc.) becomes a base64 `<img>` in the HTML.

## Return contract

| `build()` returns | Result |
|---|---|
| Non-empty string | HTML report (the one kind) |
| `None` (use `ctx.text/image/json`) | Inline chat output only, no pane |
| Anything else | Error: must return a string or `None` |

## Minimal example

```python
def build(ctx):
    return """<!doctype html>
<html>
<head><style>body { font-family: system-ui; padding: 24px; }</style></head>
<body><h1>Hello</h1></body>
</html>"""
```

## Prefer numpy over `random` / `statistics`

`numpy` and `pandas` are always available. Use them for any
sampling, distributions, or array work:

```python
import numpy as np
rng = np.random.default_rng(42)        # seeded RNG, reproducible

x = rng.normal(loc=0, scale=1, size=200)        # 200 samples N(0,1)
pick = rng.choice(["a", "b", "c"], size=10)     # with replacement (default)
pick = rng.choice(items, size=k, replace=False) # without replacement;
                                                # k must be ≤ len(items)
n = rng.integers(low=2, high=5)                 # 2..4 inclusive of low,
                                                # exclusive of high (matches range)
```

Why over stdlib `random`:

- **Bounds-safe by default.** `rng.choice(items, size=k)` uses
  replacement by default — no `ValueError` when `k > len(items)`.
  `random.sample` requires you to clamp manually.
- **Reproducible.** `np.random.default_rng(seed)` is the modern
  per-RNG seed. `random.seed()` mutates global state and can
  collide with library internals.
- **Vectorised.** `rng.normal(size=10000)` is one C call; the
  stdlib loop is 10000 Python calls.
- **Already in your deps** — no import cost beyond what you've
  already paid for `import pandas as pd`.

`random` is fine for trivial coin-flips, but anything touching
data → numpy.

## Parametric SVG via f-strings — the right pattern

Python f-strings are the idiomatic way to build reusable SVG
components. Functions like `sparkline(color, values, w=88, h=28)`
return an f-string where `{w}`, `{h}`, `{path}`, etc. expand at
call time:

```python
def sparkline(color, values, w=88, h=28):
    pts = ...
    path = "M " + " L ".join(...)
    return f'<svg width="{w}" height="{h}" ...><path d="{path}"/></svg>'
```

**Critical:** `{var}` substitution only happens inside an **f-string
literal** (`f"..."` or `f"""..."""`).  If you write a plain string
or a triple-quoted block without the `f` prefix, the `{var}`
placeholders are emitted verbatim, producing browser errors like
`Expected length, "{w}"`.  The browser still renders the page, but
the SVG element is invisible and the console fills with noise.

Rule: every SVG template that uses `{var}` placeholders **must** be
an f-string.  Use a helper function to keep the template small and
the substitution obvious.

## Two screenshot-critical rules — READ BEFORE WRITING

These bite hard if you skip them; the live render looks fine
but the screenshot (the LLM's only feedback channel) is wrong.

1. **SVG element paint must be INLINE attributes, not CSS
   classes.** html-to-image doesn't inline `<style>` rules into
   its SVG snapshot. Put `fill`/`stroke`/`stroke-width`/
   `font-size`/`text-anchor` etc. directly on each `<rect>`,
   `<text>`, `<path>`. CSS classes work for HTML elements
   (`<div>`, `<body>`) but NOT for SVG primitives.

2. **`<svg>` width/height attributes must match the actual
   content extent (matching the viewBox).** `width="100%"
   height="100%"` blows up the iframe auto-size to a
   multi-thousand-pixel-tall screenshot.

3. **No `min-height: 100vh` / `height: 100%` cascades on body
   or container.** Same failure mode as the SVG one — the
   screenshot path resizes the iframe to ~8000 px to measure
   natural content height, and any 100vh element fills it,
   producing a huge near-empty screenshot.

See `screenshot-friendly.md` for the full rule set including
three.js `preserveDrawingBuffer`, cross-origin assets, etc.

## What the BE adds to your HTML

Exactly one transformation: inject the screenshot shim into
`<head>`:

```html
<meta name="voitta-slug" content="...">
<meta name="voitta-render-id" content="...">
<script src="/api/_html_to_image.js"></script>
<script src="/api/_panel_shim.js"></script>
```

If your HTML lacks `<head>`, one is synthesised. If it lacks
`<!doctype>`, that's prepended. Your body markup ships unchanged.

## The `ctx` object

```
ctx.text(body)                — emit a Markdown block into the chat
ctx.image(data, mime, alt)    — emit an inline image (bytes or base64)
ctx.json(value)               — emit a JSON code block
ctx.log("debug", "lines")     — captured into the tool result
ctx.args                      — dict forwarded from run_script(args=)
ctx.host                      — bookmarklet's host page (e.g. "enterprise.voitta.ai")
ctx.theme()                   — dict of raw CSS variables for the active plugin
ctx.snapshot(handle)          — look up a python_storage record by handle → dict with "path"
ctx.file(handle, filename?)   — Path to a file inside a snapshot (first non-meta file if filename omitted)
ctx.dataframe(handle)         — read curves.pkl from a curves snapshot
ctx.raw(handle)               — read raw.json from a raw snapshot
ctx.ensure_local(ref)         — materialise scheme://… ref to a local path
```

That's the whole `ctx` API. Anything else you need, you write in
Python and/or in your HTML's `<script>`.

## Reading files from python_storage (e.g. veed_frame output)

Tools like `veed_frame` write files into python_storage and return a
`handle` (e.g. `py_71e1c5b2`). Three equivalent ways to read them:

**1. `ctx.file(handle)` — simplest, returns a Path:**
```python
def build(ctx):
    path = ctx.file("py_71e1c5b2")          # first file in snapshot
    data = path.read_bytes()
    img_b64 = __import__("base64").b64encode(data).decode()
    return f'<img src="data:image/jpeg;base64,{img_b64}">'
```

**2. `ctx.ensure_local("py://handle/filename")` — URI style:**
```python
def build(ctx):
    path = ctx.ensure_local("py://py_71e1c5b2/frame_0.00s.jpg")
    data = open(path, "rb").read()
    ...
```

**3. `ctx.snapshot(handle)` — raw record, use `rec["path"]` for the dir:**
```python
def build(ctx):
    rec = ctx.snapshot("py_71e1c5b2")
    import os
    snap_dir = rec["path"]
    fname = [f for f in os.listdir(snap_dir) if f.endswith(".jpg")][0]
    data = open(os.path.join(snap_dir, fname), "rb").read()
    ...
```

Use option 1 unless you need the raw metadata.

## ctx.theme()

Returns a dict of raw CSS variables for the active plugin. Keys
are real CSS-variable names (`--voitta-bg`, `--voitta-accent`,
`--voitta-flow-edge-success`, etc.). Substitute into your CSS
directly:

```python
def build(ctx):
    t = ctx.theme()
    vars_block = "".join(f"  {k}: {v};\n" for k, v in t.items())
    return f"""<!doctype html>
<html>
<head><style>
  :root {{
{vars_block}  }}
  body {{ background: var(--voitta-bg); color: var(--voitta-text);
          font-family: system-ui; padding: 24px; }}
  .accent {{ color: var(--voitta-accent); }}
</style></head>
<body><h1 class="accent">Themed report</h1></body>
</html>"""
```

## Composition

Mix anything inside one HTML document. The system makes no
assumptions — the iframe is yours.

- See `recipes/plotly.md` for embedding interactive Plotly charts
- See `recipes/elk.md` for ELK diagrams (load elkjs from CDN,
  layout in the iframe, paint SVG)
- See `recipes/matplotlib.md` for server-side matplotlib → `<img>`
- See `recipes/three.md` for three.js scenes
- See `recipes/tables.md` for HTML tables and KPI cards
- See `recipes/mermaid.md` for mermaid diagrams
- See `screenshot-friendly.md` for "screenshot is the LLM's only
  feedback loop, write to screenshot well"
- See `elk-design-templates.md` for three coordinated ELK style
  families (schematic / energy-monitor / hybrid) + standalone
  patterns

## Lifecycle

1. **Author**: `define_script(name, code)` — smoke-tests
   `build(ctx)` before persisting; bad code is rejected with the
   traceback so you can fix and retry. Errors on duplicate names —
   always `list_scripts` first.
2. **Edit**: `edit_script(name, patches=[{find, replace}])` —
   applies search-replace patches; same smoke-test gate.
3. **Read**: `get_script(name)` — returns the source verbatim.
4. **Run**: `run_script(name, args={})` — executes `build(ctx)`,
   mounts the result.
5. **List**: `list_scripts()` — every script you've authored.
6. **Delete**: `delete_script(name)`.

### **Iterate by editing, not by creating new scripts**

The most common LLM anti-pattern: bug or visual tweak →
`define_script("my-report-v2", ...)` → next attempt
`define_script("my-report-v3", ...)`. Don't do this.

When you're iterating on a report:

1. `get_script("my-report")` — read the current code
2. `edit_script("my-report", patches=[{find: "...", replace: "..."}])`
3. `run_script("my-report")` to verify

The user sees ONE named report that improves with each turn,
not a graveyard of dead drafts. `edit_script` runs the same
smoke-test gate as `define_script` so you still get the
traceback if your fix is wrong.

`define_script` is for genuinely new reports — different
concept, fresh start. Not for retrying after an error.

## Inspection

- **`verify_script(name)`** — last render's inventory (lightweight).
- **`get_script_errors(name)`** — render-event log.
- **`screenshot_report()`** — capture the currently-mounted iframe.

## Storage

- Source: `scripts/<name>/code.py`
- Render state: `scripts_state/<name>/`

## Sandboxing

Scripts run in-process — no subprocess isolation. The user is the
developer; this is a local tool. Don't run scripts from untrusted
sources.
