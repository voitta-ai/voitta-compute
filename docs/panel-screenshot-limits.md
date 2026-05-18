# Panel report — screenshot limits

`screenshot_report` returns a PNG of the report iframe. It is **lossy** in specific, known ways. Treat it as approximate layout verification, not pixel-exact rendering. When you need ground truth, ask the user.

## Known blindnesses

### 1. Cross-origin iframes inside the report render as blank rectangles

`screenshot_report` uses `html2canvas` running in the parent Bokeh document. It walks the DOM and re-rasterises every node — but it cannot pierce a cross-origin iframe boundary. The iframe shows up as a blank rectangle of the iframe's background colour.

This affects:
- Custom `<iframe srcdoc>` content the LLM wrote itself (embedded D3 + custom HTML, observable plots, etc.).
- Embedded third-party iframes (Google Sheets embeds, Figma embeds, etc.).

**`ctx.three_scene(...)` IS captured.** The shim walks shadow roots to find each three_scene iframe, requests its canvas pixels via postMessage (`voitta_three_capture` → `voitta_three_capture_response`), and composites the result. The renderer is configured with `preserveDrawingBuffer: true` so `canvas.toDataURL()` returns real pixels. Verified end-to-end on cold CDN load and re-show.

The response includes `nested_scenes_captured`. Non-zero = scenes captured. If `0` with three_scene panes present, the scene area in the screenshot is blank — usually means the scene is still loading; retry in a moment or use `verify_report` for structural confirmation.

Plain Panel content (Markdown, Tabulator, matplotlib PNG panes, Plotly) is captured fine — those render directly in the report's own document, no nested iframe.

### 2. CSS loaded just-in-time may not be applied at capture time

Widget stylesheets (Tabulator, SlickGrid, Bokeh widget styles) sometimes load asynchronously after the initial paint. If `screenshot_report` fires immediately after the report mounts, the screenshot may show unstyled HTML the user never actually sees.

Mitigation: don't screenshot in the same tool call as `show_holoviz_report`. Give the user (and you) at least one turn to see the rendered state before screenshotting.

### 3. Fonts loaded asynchronously fall back to system defaults in the capture

`html2canvas` rasterises text via the canvas API, which can't await `document.fonts.ready`. Custom webfonts that haven't fully loaded at capture time fall back to system fonts in the screenshot, even when they render correctly live.

### 4. WebGL canvases need `preserveDrawingBuffer: true` to be captured

By default, WebGL clears the drawing buffer after compositing. Even when the WebGL canvas is in the same document as `html2canvas` (rare with `ctx.three_scene`'s sandbox), the captured pixels are empty. `ctx.three_scene` sets `preserveDrawingBuffer: true` for this reason — but the cross-origin barrier (item 1) still blocks the capture.

### 5. The screenshot is 1–2 animation frames behind on-screen state

For animated content (rotating 3D, Plotly transitions, Bokeh widget hovers), the screenshot is a snapshot of a slightly earlier frame than what the user sees. Static content is unaffected.

### 6. The widget's shadow-DOM theme tokens don't cascade into the iframe

The chat widget injects `--voitta-*` tokens into its own shadow DOM. The report iframe is a separate document with its own `<head>` — those tokens don't cross. Without `ctx.apply_theme(layout, host=…)` in `build`, the screenshot shows Panel's default light theme even on a dark host.

## What to use instead when these matter

- **Verifying a Three.js / WebGL report:** ask the user. "Do you see the rotating cube?" "What colours?" Don't read a blank screenshot and rewrite working code.
- **Verifying styling:** `ctx.log(ctx.theme_css(host=…))` inside `build` prints the actual CSS being injected. Compare against what you expect.
- **Verifying structure:** `verify_report(report_id)` returns the rendered Panel root inventory (pane types, counts, bounding boxes). Smaller and more reliable than a screenshot when you just need to know "did I get 3 plots or 2."

## When the screenshot IS reliable

- Static matplotlib / Plotly / Markdown / Tabulator content in a plain (non-three_scene) layout.
- After the report has been visible for at least one turn (CSS and fonts have settled).
- With `ctx.apply_theme(layout, host=…)` applied (so the screenshot shows the host theme, not Panel's default).
