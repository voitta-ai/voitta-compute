# Recipe index

Copy-paste patterns for embedding common content types inside an
HTML report. Every recipe assumes the script's `build(ctx)`
returns a raw HTML string — see `../06-reports.md` for the
contract.

| File | What |
|---|---|
| `plotly.md` | Interactive Plotly charts via CDN |
| `elk.md` | ELK diagrams — minimal layout + paint pattern |
| `elk-energy-monitor.md` | **Full proven recipe**: dark control-room theme, per-type SVG glyphs, marching-dash animated edges, grid-wrapped layout |
| `matplotlib.md` | Server-side matplotlib → base64 `<img>` |
| `three.md` | Three.js scenes (with `preserveDrawingBuffer` for screenshots) |
| `mermaid.md` | Mermaid flow/sequence/state diagrams |
| `tables.md` | HTML tables and KPI cards |
| `interactivity.md` | Vanilla JS, htmx, Alpine.js for interactive widgets |
| `theming.md` | Using `ctx.theme()` CSS variables |

For ELK specifically — coordinated style families (schematic,
energy-monitor, hybrid) and standalone patterns (dashed
connectors, gradient fills, KPI cards, foreignObject HTML) — see
`../elk-design-templates.md`.
