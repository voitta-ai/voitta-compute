# Recipe index

Copy-paste patterns for common content types inside HTML reports. Every recipe assumes `build(ctx)` returns a raw HTML string.

| Recipe | Description |
|---|---|
| `matplotlib.md` | Server-side matplotlib chart → base64 `<img>` |
| `plotly.md` | Interactive Plotly chart via CDN |
| `three.md` | Three.js WebGL scene via CDN, with `preserveDrawingBuffer` |
| `mermaid.md` | Mermaid diagrams (flow, sequence, ER, etc.) via CDN |
| `tables.md` | HTML tables and KPI cards, themed |
| `interactivity.md` | Vanilla JS and Alpine.js patterns |
| `elk.md` | ELK graph layout via CDN |
| `theming.md` | Using `ctx.theme()` CSS variables |
| `knowledge-graph.md` | networkx → ELK/SVG visualization |

## Rules that apply to all recipes

- Return a **complete** HTML document string (with `<html>`, `<head>`, `<body>`), or at minimum a self-contained fragment. The iframe has no shared styles with the host page.
- No `100vh` / `100vw` — breaks screenshot capture (see `../screenshot-friendly.md`).
- SVG `fill`/`stroke` must be inline attributes, not CSS, for screenshots.
- Three.js needs `preserveDrawingBuffer: true`.
- CDN scripts load at runtime in the iframe — they work fine for interactive use. Screenshots capture whatever is rendered at capture time.
