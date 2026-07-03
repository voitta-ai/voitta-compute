# Recipe: report → identical vector PDF (fpdf2)

How to rebuild an HTML report as a **true vector PDF** — selectable text, crisp
scalable art, *not* a screenshot. Distilled from a real session that reproduced
a two-panel SVG+text report 1:1 (verified against a rasterized preview).

Unlike the other recipes, `build(ctx)` here returns `None`: the deliverable is a
PDF **file**, not report-pane HTML.

## Decide the route first: read the report source

Before picking any engine, `get_script(name)` the report and check what it's
made of. This one decision determines everything:

- **Pure HTML + CSS + SVG, zero JavaScript** → no browser needed at all.
  Rebuild natively with fpdf2 (this recipe). The SVG embeds as real vectors;
  text becomes native selectable PDF text.
- **JS-drawn visuals (Plotly, three.js, mermaid, ELK via CDN)** → fpdf2 cannot
  execute JS. Render each visual to a static image (matplotlib rebuild, or
  `fig.to_image()` for Plotly) and embed the PNGs; or fall back to
  `screenshot_report` for the JS parts. There is no headless-browser route in
  the sandbox (see below).

## Engine reality in the sandbox (macOS arm64, no C compiler)

Verified by an actual import + tiny-render probe — don't trust "it pip-installs"
as "it works":

| Engine | Verdict | Why |
|---|---|---|
| **fpdf2** | ✅ **use this** | Installed by default, pure-Python. Its SVG importer parses `linearGradient`/`radialGradient` — gradients survive as real vector gradients. |
| svglib + reportlab | ✅ works | Pure-Python fallback; produced a valid vector PDF in the probe. fpdf2 is preferred (documented, RAG-indexed under `lib-sources/fpdf2/`). |
| PyMuPDF (`fitz`) | ✅ installs | arm64 wheel, no compile. Use it to **rasterize the PDF for visual verification**, not to build. |
| weasyprint | ❌ fails | pip-installs fine, then `import` dies: needs native `libgobject-2.0`, absent, no compiler to build it. |
| cairosvg | ❌ fails | Same story with `libcairo`. |
| Headless browser | ❌ absent | No `chromium`, `google-chrome`, or `wkhtmltopdf` binaries on the box. |

**Important: every app upgrade wipes `userbase/`**, so pip-installed engines
(reportlab, svglib, pymupdf) vanish between versions — only bundled deps
(fpdf2, matplotlib) persist. So DO re-probe the *viable* engines at the start
of a PDF task, but do NOT re-test weasyprint/cairosvg/headless-browser: their
failure is structural (missing native libs, no compiler, no browser binary),
not sandbox state — pip reinstalling them will never help.

Probe pattern (viable engines only — cheap to re-run after any app upgrade):

```python
import importlib

def build(ctx):
    report = {}
    for mod in ["fpdf", "svglib", "reportlab", "fitz"]:
        try:
            importlib.import_module(mod)
            report[mod] = "import-ok"
        except Exception as e:
            report[mod] = f"MISSING: {type(e).__name__}"
    ctx.json(report)
    # fpdf should always be import-ok (bundled). Missing svglib/reportlab/fitz
    # just means pip_install them again — the last upgrade wiped userbase/.
```

## The core pattern

Three non-obvious pieces, then plain fpdf2 layout:

### 1. Embed an SVG as vectors at an exact position

`pdf.image(svg)` is not the way when you need precise placement inside a panel.
Use the SVG object API and translate the path group:

```python
from fpdf import FPDF
from fpdf.svg import SVGObject
from fpdf.drawing import Transform

svg = SVGObject(svg_string)              # gradients parse in this version
_, _, paths = svg.transform_to_rect_viewport(
    pdf.k, target_w, target_h, align_viewbox=False, ignore_svg_top_attrs=True)
paths.transform = paths.transform @ Transform.translation(x, y)
pdf.draw_path(paths)
```

### 2. Never rely on SVG `<text>` — render text natively

fpdf2's SVG importer is **weak on `<text>` elements** (glyphs drop or misplace).
Strip text out of the SVG and draw it with `pdf.text()` — that's also what makes
it selectable/searchable. For Cyrillic/Greek/CJK, the built-in fonts render
blank boxes; register DejaVu, which ships inside matplotlib:

```python
from pathlib import Path
import matplotlib

ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
pdf.add_font("DejaVu", "",  str(ttf / "DejaVuSans.ttf"))
pdf.add_font("DejaVu", "B", str(ttf / "DejaVuSans-Bold.ttf"))
pdf.add_font("DejaVu", "I", str(ttf / "DejaVuSans-Oblique.ttf"))
pdf.set_font("DejaVu", "", 20)
```

### 3. Mirror the report's geometry and theme

Work in points with a custom page size so report pixels map 1:1
(`FPDF(unit="pt", format=(W, H))`, `set_auto_page_break(False)`), and pull
colors from `ctx.theme()` so the PDF matches the report's palette:

```python
t = ctx.theme() or {}
bg     = t.get("--voitta-bg",     "#1a1424")
accent = t.get("--voitta-accent", "#c8a35a")
# fpdf2 wants RGB tuples:
def _hex(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
pdf.set_fill_color(*_hex(bg)); pdf.rect(0, 0, W, H, style="F")
```

## Verify by rasterizing — with the token-limit gotcha

Always eyeball the result. Rasterize page 1 with PyMuPDF, **write the full-size
preview to a file**, and inline only a small downscaled JPEG. A full-resolution
base64 PNG through `ctx.image` blows past the tool-result token limit and the
run's output gets truncated; the `Read` tool may also be unavailable for
`/tmp/*.png`, so the tiny inline JPEG is the reliable feedback channel:

```python
import io, os, fitz
from PIL import Image

out = ctx.args.get("out", "/tmp/report.pdf")
pdf.output(out)

preview = "/tmp/report-preview.png"
doc = fitz.open(out)
pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.62, 0.62))
pix.save(preview)                                  # full preview → file

img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
img = img.resize((460, int(img.height * 460 / img.width)))
jb = io.BytesIO(); img.save(jb, format="JPEG", quality=55)
ctx.image(jb.getvalue(), "image/jpeg", "PDF preview")  # tiny inline → chat

ctx.json({"path": out, "bytes": os.path.getsize(out), "preview": preview})
```

## End-to-end workflow (the tool-call sequence that worked)

1. `get_active_report()` → `get_script(report_name)` — read the source, decide
   the route (JS or no JS).
2. `run_script("pdf-engine-probe")` — confirm what's importable *right now*
   (define the probe above if it doesn't exist).
3. `pip_install(["pymupdf"])` — for the verification rasterizer (and
   `svglib`/`reportlab` if you want the fallback engine). These must be
   reinstalled after every app upgrade — the userbase wipe removes them.
   Skip weasyprint/cairosvg entirely; they can never import here.
4. Consult RAG before writing code: `rag_query(corpus="docs", ...)` for the
   ctx/delivery contract, `rag_query(corpus="code", query="fpdf2 svg ...")` for
   exact fpdf2 APIs (source is indexed under `lib-sources/fpdf2/`).
5. `define_script(name, folder_name=..., code=...)` — put the builder next to
   the report it exports; accept `out`/`preview` via `ctx.args` so it's
   re-runnable to any path.
6. `run_script` → look at the inline preview → `edit_script` → repeat until the
   preview matches the report.

A complete working example (two-panel layout, vector SVG illustration with
gradients, styled selectable Cyrillic poem, themed panels, preview loop) lives
in the workspace as `onegin-poem/onegin-poem-pdf`.

## Known fidelity limits

- SVG `<text>` inside the embedded illustration may drop glyphs — move all text
  to native `pdf.text()` calls.
- Hairline decorative CSS (flourishes, text-shadow, blur filters) has no fpdf2
  equivalent — approximate with rules/shapes or accept the loss.
- CSS layout is not interpreted at all: you are *rebuilding* the layout in
  points, not converting it. Measure the report's proportions and reproduce
  them (e.g. panel widths as fractions of the page, 1 px ≡ 1 pt).
