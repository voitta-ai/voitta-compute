# PDF export of reports

There are **two distinct ways** to produce a PDF; pick by what you start from:

1. **Report → PDF export (HTML → PDF).** The report pane's **Download PDF**
   button converts the *currently active* report's HTML to PDF. Zero code — the
   user clicks it. This is the common path and the main subject of this page.
   Engine: **xhtml2pdf**.
2. **Build a PDF programmatically (no HTML).** A script constructs a PDF
   directly — pages, text, tables, images, vector drawing — with **fpdf2** (or
   the heavier **ReportLab**, already installed). Use this when you want a PDF as
   a *deliverable file* rather than an on-screen report, or when HTML/CSS can't
   express the layout. See [Building a PDF directly](#building-a-pdf-directly).

The rest of this page is about path 1 (the Download button): how it works and —
importantly — **what renders and what does not**, so you can author reports that
export cleanly.

## Mechanics

```
Download PDF button (report pane header)
        │  GET /api/report/export-pdf?id=<slug>&render_id=<rid>&title=<name>
        ▼
backend route → app.services.report_pdf.render_report_pdf(slug, render_id)
        │  pulls the cached report HTML (same bytes the iframe shows)
        ▼
xhtml2pdf (pure-Python, ReportLab under the hood) → PDF bytes
        ▼
browser downloads "<title>.pdf"
```

- The export converts the **exact HTML the report already produced** (the cached
  `/api/html-report` body), so what you `return` from `build(ctx)` is what gets
  converted.
- The engine is **[xhtml2pdf](https://github.com/xhtml2pdf/xhtml2pdf)** — chosen
  because it is pure-Python (installs as a wheel via the lazy installer, no
  native libraries, works in the packaged app) and produces **selectable,
  copy-pasteable text** (not a screenshot). Its source is indexed into the
  `code` RAG corpus under `lib-sources/xhtml2pdf/` — `rag_query(..., corpus="code")`
  for `default.py` / `tags.py` / `parser.py` to confirm any specific tag or CSS
  property.

## The one hard limitation: no JavaScript

xhtml2pdf **does not run JavaScript.** It parses static HTML/CSS only. This is the
single most important thing to know:

- **A report whose visuals are drawn client-side by JS will export blank/empty
  where those visuals are.** That includes **Bokeh, Plotly, and three.js** — they
  paint into a `<canvas>`/WebGL context *in the browser*, and none of that exists
  when xhtml2pdf reads the HTML.
- The interactive in-app report pane remains the source of truth for those. The
  PDF is for the **textual/tabular** content.

### How to make a chart export

Render the chart to a **static image server-side** and embed it as an `<img>`, in
addition to (or instead of) the interactive version:

```python
def build(ctx):
    import base64, io
    import matplotlib
    matplotlib.use("Agg")            # already the default in report scripts
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 1, 3])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    b64 = base64.b64encode(buf.getvalue()).decode()
    # data: URIs embed inline and DO export.
    return f'<html><body><h1>Q3</h1><img src="data:image/png;base64,{b64}"></body></html>'
```

- **matplotlib PNGs export perfectly** (they're just images).
- Plotly can emit a static PNG via `fig.to_image(format="png")` (needs `kaleido`);
  Bokeh via `export_png` (needs a browser driver — heavier). A `matplotlib`
  fallback is the reliable path.

## What renders well

- **Text**: headings `h1`–`h6`, `p`, `br`, `hr`, `sub`, `sup`, `a` links, `font`.
- **Lists**: `ul`, `ol`, `li`.
- **Tables**: `table`, `tr`, `td` (and `th`) — solid, with borders/padding/
  background. This is xhtml2pdf's strong suit; prefer tables for structured data.
- **Images**: `<img>` with `data:` URIs (inline base64) or same-origin/relative
  URLs (the exporter fetches those from the backend). External image URLs are not
  fetched.
- **Basic CSS**: `color`, `background-color`, `font-family`/`font-size`/
  `font-weight`/`font-style`, `text-align`, `margin`/`padding`, `border`,
  `width`/`height` in absolute units, inline `style=` and `<style>` blocks.
- **Page control** (xhtml2pdf extensions, useful for multi-page reports):
  `page-break-before`/`page-break-after`, `-pdf-keep-with-next`, and `@page` /
  `@frame` rules for page size, margins, and running headers/footers. Special
  tags `pdfpagenumber`, `pdfpagecount`, and `pdftoc` (table of contents) work.

## What does NOT render (avoid or provide a fallback)

- **JavaScript** — see above. Anything computed/drawn at runtime is gone.
- **Modern layout CSS**: **no flexbox** (`display: flex`), **no CSS grid**
  (`display: grid`), **no `position: absolute/fixed/sticky`**. Reports built with
  these will collapse to normal block flow — often looking broken. Use tables or
  simple block/inline layout for anything that must export.
- **CSS custom properties (variables)** — `var(--x)` is not resolved. Note
  `ctx.theme()` returns CSS variables; if you rely on them via `var()` the PDF
  won't pick up those colors. For export-critical styling, inline concrete
  values.
- **Transforms, transitions, animations, filters, `box-shadow`, gradients** —
  ignored.
- **Web fonts** loaded via `@font-face` from a URL — stick to standard families
  (Helvetica, Times, Courier) unless a font is explicitly registered.
- **Viewport units** (`vw`/`vh`) and complex `calc()` — unreliable; use absolute
  units (`pt`, `in`, `cm`, `px`) or `%`.

## Authoring guidance (summary)

To make a report that both looks good in-app **and** exports cleanly:

1. Structure content with **tables and block elements**, not fl* layout.
2. For any chart, also emit a **static image** (matplotlib PNG as a `data:` URI).
3. Use **inline/concrete CSS values** for export-critical color and spacing;
   don't depend on `var()`.
4. Test the export from the report pane — if a section is empty in the PDF, it
   was almost certainly JS-drawn or flex/grid-laid-out.

## Building a PDF directly

When you want a PDF **file** built from scratch (not an HTML report converted),
use **fpdf2** — a pure-Python, generic PDF builder. It's installed by default and
its source + docs are indexed in the `code` RAG corpus under `lib-sources/fpdf2/`
(`rag_query(..., corpus="code")` for the `FPDF` API, `table.py`, `svg.py`, and the
`docs/`/`tutorial/` guides).

```python
def build(ctx):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=16)
    pdf.cell(0, 10, "Quarterly Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    with pdf.table() as table:
        for row in (("Region", "Revenue"), ("EMEA", "$1.2M"), ("APAC", "$0.9M")):
            r = table.row()
            for cell in row:
                r.cell(cell)

    pdf_bytes = bytes(pdf.output())         # in-memory PDF bytes
    # Hand the file to the user via the workspace, or preview inline:
    ctx.log(f"built {len(pdf_bytes)} byte PDF")
    return None
```

Why fpdf2 over xhtml2pdf here: **fpdf2 executes real Python** to lay the document
out, so nothing depends on HTML/CSS quirks — tables, positioning, fonts, images,
and vector graphics all work deterministically. Text stays selectable. The
trade-off is you write layout code instead of HTML.

**ReportLab** is also available (it's xhtml2pdf's engine) if you need its more
advanced typesetting (Platypus flowables, precise flow control) — but its source
is *not* in `lib-sources`, so you're working from prior knowledge, not RAG.

## Failure modes the user may see

- **"The PDF export engine (xhtml2pdf) isn't installed yet"** — it installs on
  first launch with the other heavy deps; retry shortly.
- **"This report isn't cached anymore — re-run the script"** — the in-memory
  render cache dropped this `(slug, render_id)`; re-run the script, then export.
- **"PDF conversion failed …"** — the HTML/CSS used something the engine can't
  parse; simplify the markup (usually removing flex/grid or malformed CSS fixes
  it).
