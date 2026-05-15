# Theming architecture — read this when your skin won't stick

This document is for the case where you've tried `ctx.apply_theme(...)`, `ctx.get_theme(...)`, and explicit `ctx.add_css(...)`, and **something still looks wrong** — a stubbornly-white background, a Tabulator that refuses to follow the palette, a font that won't change.

Two goals:
1. Show you exactly how theming flows end-to-end so you can localise the failure.
2. Tell you which failure classes are **our bug** (please file an issue) vs. which are **structural limits** worth knowing about.

> Our goal is that **every surface in a report follows the active theme by default** — when one doesn't, that's the bug. Open an issue with the symptom + DevTools Computed-pane screenshot + a minimal `build(ctx)`. We'd rather hear "your wrapper missed `.bk-Foo-bar`" than have plugin authors invent local workarounds that bit-rot.

---

## The flat model — one CSS string, two injection channels

Theming used to have a hidden post-build server transform: `ctx.apply_theme` stamped tokens onto the layout, the template wrapper read them back, and `_build_theme_css` ran in the wrapper. That path produced CSS for outer-document widgets fine but **silently missed shadow-DOM widgets** (Tabulator, Bokeh DataTable, Flatpickr-backed date pickers) — those have their own per-component shadow root that outer-document CSS can't pierce. Reports looked half-themed.

The current model is flat. No hidden transforms. The LLM gets a CSS string and decides where to put it:

```
plugin's theme.css                   frontend/src/theme.css
  (e.g. --voitta-accent: #0a84ff)      (core defaults)
              │                                 │
              └──────► theme.py resolver ◄──────┘
                       (merge: plugin wins for any key it sets)
                               │
                               ▼
                ┌───────────────────────────────┐
                │ ctx.get_theme(host=…)         │
                │   → {raw_tokens, palette,     │
                │      is_dark, css_snippet, …} │
                │                               │
                │ ctx.theme_css(host=…)         │
                │   → str (full CSS, ~7 KB)     │
                │     :root, :host { --… }      │
                │     html, body { … }          │
                │     .bk-Markdown { … }        │
                │     .tabulator { … }          │
                │     …                         │
                └──────────────┬────────────────┘
                               │
              ┌────────────────┴─────────────────┐
              ▼                                   ▼
   ctx.add_css(css)                   pn.widgets.Tabulator(
        │                                  df,
        ▼                                  stylesheets=[css],   ← shadow root
   template.config.raw_css            )
        │
        ▼
   <head><style>…</style></head>      ← outer document
        │
   reaches: Markdown, Card, body,
            html, input, .bk-Tabs,
            Plotly modebar, …
```

Two channels, both fed by the **same CSS string**:

* **Outer document `<head>`** — via `ctx.add_css(css)` (which appends to `template.config.raw_css`). Reaches anything that isn't behind a shadow boundary: Markdown panes, Card containers, template chrome, plain `<input>`, the iframe body.

* **Per-widget shadow root** — via `stylesheets=[css]` on the widget. Reaches widgets whose chrome lives behind Bokeh's per-component shadow boundary: `pn.widgets.Tabulator`, Bokeh `DataTable`, the date pickers.

The same `theme_css(...)` string works for both because the variable block uses `:root, :host { --voitta-…: …; }`. `:root` matches in the outer doc; `:host` matches in any shadow root. CSS custom properties inherit through the shadow boundary; selector rules don't.

## API — three calls, no hidden state

```python
def build(ctx):
    # 1. Get the CSS string for the active theme.
    css = ctx.theme_css(host="enterprise.voitta.ai")

    # 2. Apply to the outer document.
    ctx.add_css(css)

    # 3. Apply to shadow-DOM widgets explicitly via stylesheets=.
    table = pn.widgets.Tabulator(
        df,
        sizing_mode="stretch_width",
        layout="fit_columns",
        stylesheets=[css],
    )

    return pn.Column(pn.pane.Markdown("# Title"), table)
```

`ctx.apply_theme(layout, host=…)` is a 4-line convenience that does steps 1-3 for you — it walks the layout, finds the shadow-DOM widget classes (Tabulator, DataTable, DatePicker, DatetimePicker, DateRangePicker, DatetimeRangePicker), and appends `css` to each one's `stylesheets`. Use it when you're happy with the defaults; reach for the explicit three-line form when you want one widget unthemed, want different overrides per widget, etc.

There's no hidden post-build server transform, no magic attribute stamped on the layout. The `ctx` your `build()` mutated is passed *explicitly* into `_wrap_template` — its `_raw_css`, `_js_files`, `_design`, `_template_theme` slots are read directly. The CSS is just text — the LLM can `print(ctx.theme_css(host=...))` and see exactly what's about to be injected, or `ctx.log(ctx.theme_css(host=...))` to surface it in the tool result.

### Three orthogonal axes (Panel-native)

Theming has three independent layers, all opt-in:

1. **Design** — `ctx.set_design("material"|"bootstrap"|"fast"|"native")`. Picks a Panel-native widget chrome family. Sets per-class modifiers (Tabulator's built-in theme, Card padding, button styling) automatically. Default: unset → Panel's bare defaults.
2. **Template theme** — `ctx.set_template_theme("default"|"dark")`. Drives Panel's light/dark scheme on the template wrapper (header bg, sidebar fill, bundled Bokeh figure colours). Default: unset → Panel's `default`.
3. **Tokens** — `ctx.apply_theme(layout, host=…)`. Overlays the host's `--voitta-*` palette on top of (1) and (2). This is YOUR palette winning over Panel's.

The three compose cleanly because they target different selectors:

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ Design          │  │ Template theme  │  │ apply_theme     │
│                 │  │                 │  │                 │
│ Tabulator chr.  │  │ #header bg      │  │ --voitta-*      │
│ Button shape    │  │ Bokeh fig bg    │  │ surfaces        │
│ Card padding    │  │ Sidebar fill    │  │ accents         │
│ Slider styling  │  │                 │  │ typography      │
│ Form chrome     │  │                 │  │ Markdown text   │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

For a fully-coordinated dark report on a dark host: set all three.

## Surfaces — what gets themed automatically

| Surface | Channel | Token(s) |
|---|---|---|
| `<body>` background + text | outer doc | `--voitta-bg`, `--voitta-text`, `--voitta-font-sans` |
| `#container` / `#main` / `#content` | outer doc | `--voitta-bg`, `--voitta-text` |
| `.bk-Markdown` headings, body, links, code, blockquote, table borders | outer doc | `--voitta-text`, `--voitta-link-fg`, `--voitta-code-bg`, `--voitta-divider` |
| `.bk-Card` / `.pn-card` containers + headers | outer doc | `--voitta-surface`, `--voitta-border`, `--voitta-divider` |
| `.tabulator` header, rows (with even-row stripe), hover, footer | shadow root via `stylesheets=[css]` | `--voitta-surface`, `--voitta-bg`, `--voitta-text`, `--voitta-divider` |
| `<input>` / `<textarea>` / `<select>` + focus ring | outer doc | `--voitta-bg`, `--voitta-border`, `--voitta-accent` |
| `button.bk-btn` (default + `.bk-btn-primary`) | outer doc | `--voitta-surface`, `--voitta-accent`, `--voitta-accent-fg` |
| `.bk-Tabs` active/inactive | outer doc | `--voitta-surface`, `--voitta-bg`, `--voitta-accent` |
| Plotly modebar | outer doc | `--voitta-text-muted`, `--voitta-text` |
| Scrollbars | outer doc | `--voitta-scrollbar-thumb`, `--voitta-scrollbar-track` |

## Surfaces you still own

- **Pixel content of matplotlib `Figure`** — set `plt.rcParams` before plotting, or pass colors per `plot()` call. Pull palette from `ctx.get_theme(host=…)["palette"]`.
- **Plotly** `paper_bgcolor` / `plot_bgcolor` / `font_color` — `fig.update_layout(...)`. Use `theme["is_dark"]` for the template (`plotly_dark` vs `plotly_white`).
- **Three.js scene** `bg` and material colors — `ctx.three_scene(scene_js, bg=…)`.
- **`pn.pane.HTML` content** with inline `style="..."` attributes — inline styles beat stylesheets in the cascade. Don't bake hardcoded colours; template from `ctx.get_theme()` instead.
- **Bokeh figure glyphs** (lines, bars, dots) — set on the figure at construction time.

---

## Known limits

### Limit 1: Inline `style=` attrs win the cascade

If the LLM writes `<div style="background:#fff">` inside a `pn.pane.HTML`, the wrapper's `body { background: var(--voitta-bg) }` rule loses — inline styles have higher specificity than stylesheet rules. Fix locally: don't bake hardcoded colours into HTML content; pull from `ctx.get_theme()` and template the colour in.

### Limit 2: Iframe `srcdoc` documents are independent

Anything that renders in its own `<iframe srcdoc>` (notably `ctx.three_scene`) is a separate document with its own `<head>` — the parent's `:root` variables don't cross the iframe boundary. `ctx.three_scene` takes a `bg=` parameter for this reason. Custom `<iframe srcdoc>` content you write needs its own `<style>` block; substitute palette values via Python f-string at build time.

### Limit 3: `pn.pane.HTML('<style>…')` does nothing

Panel sanitises HTML panes — `<style>` text gets entity-encoded and renders as literal characters inside a `<div>`, not as a real stylesheet. Use `ctx.add_css(css_text)` instead. See [07-report-scripts.md § Selector overrides](07-report-scripts.md) for the side-by-side rendered-DOM comparison.

### Limit 4: Shadow-DOM widgets need explicit stylesheets

A surprising fraction of Panel surfaces render into per-component shadow roots — not just interactive widgets but also content panes:

- **Widgets**: Tabulator, Bokeh DataTable, the Flatpickr-backed date pickers (DatePicker, DatetimePicker, DateRangePicker, DatetimeRangePicker).
- **Content panes**: Markdown, HTML, Str — Bokeh's MarkupView wraps content in a shadow root and pulls in Panel's bundled `markdown.css`.

CSS custom properties (`--voitta-*`, `--panel-*`) inherit through the shadow boundary, but **selector rules do not**. `ctx.apply_theme` handles all of these automatically by walking the layout and attaching the theme CSS to each instance's `stylesheets=` list; if you skip `apply_theme` and only call `ctx.add_css`, the shadow-DOM components keep whatever Panel's bundled stylesheets give them — which on a dark host is typically black-on-dark for Markdown body text (`body { color: var(--background-text-color) }` resolving to the user-agent default of black). The `--panel-*` bridge in `build_theme_css` covers most of these cases; explicit `stylesheets=[css]` covers the rest.

If you discover a new shadow-DOM component class that needs styling, the fix is one line in `_shadow_dom_widget_classes()` in `backend/app/services/scripts.py` — file an issue with the class name.

### How the `--panel-*` bridge works

Panel's bundled `native.css` defines the body color through a fallback chain:

```css
:root, :host {
  --background-text-color: var(--design-background-text-color,
                            var(--vscode-editor-foreground,
                            var(--jp-widgets-label-color,
                            var(--pst-color-text-base,
                            var(--panel-on-background-color)))));
}
body { color: var(--background-text-color); }
```

Panel expects the host environment (JupyterLab / VSCode / pydata-sphinx-theme) to define one of the upstream tokens. In our iframe nobody does, so the chain falls through to `var(--panel-on-background-color)` which was previously unset → user-agent default of black.

`build_theme_css` now sets the four bottom-of-chain tokens to point at our `--voitta-*` equivalents:

```css
:root, :host {
  --panel-background-color:    var(--voitta-bg);
  --panel-on-background-color: var(--voitta-text);
  --panel-surface-color:       var(--voitta-surface);
  --panel-on-surface-color:    var(--voitta-text);
}
```

That single bridge fixes every Panel-bundled stylesheet's colour resolution in one shot — no selector chasing required. It's the reason most Markdown body text issues "just work" after `ctx.apply_theme`.

### Limit 5: Plotly modebar SVG icons have hardcoded colours

We theme the bar's background to transparent and `path` fills to `--voitta-text-muted`. Some Plotly versions add icons via `<image>` tags instead of `<path>`, and image fills can't be re-coloured via CSS. If your modebar looks off, this is likely the cause.

---

## When something looks wrong — how to localise the bug

1. **Open DevTools → Elements** on the report iframe. Click the off-colour element. Look at the Computed pane.
   - Is the element receiving any `--voitta-…` variable? If yes, the variable is reaching it; the wrapper rule probably doesn't target this element.
   - If no `--voitta-…` is anywhere in Computed, the wrapper CSS didn't apply — likely Limit 4 (shadow DOM with no `stylesheets=`).
2. **Check whether you're inside a shadow root.** Look at the breadcrumb at the top of the Elements pane — if you see `#shadow-root` between the element and the iframe root, you're inside shadow DOM. The outer-document `<head>` CSS doesn't reach you. Either use `ctx.apply_theme` (which handles this) or pass `stylesheets=[ctx.theme_css(...)]` to the widget directly.
3. **Search `<head>` for `<style>` tags** containing `:root, :host {`. If you don't see them, `ctx.add_css` (or `ctx.apply_theme`) wasn't called. If you do see them but the element still isn't themed, you're hitting Limit 1 (inline style) or Limit 4 (shadow root).
4. **Check Computed → Inherited.** Did the offending element inherit `color`/`background` from a parent that's already themed? If yes, look up the parent tree for an inline `style=` (Limit 1).
5. **Search the rendered HTML for `style="`.** Inline styles win. Track down where it's set — usually a hardcoded colour in `pn.pane.HTML` content or a Bokeh property like `figure.background_fill_color`.
6. **Print the CSS that's being injected.** Inside `build(ctx)`: `ctx.log(ctx.theme_css(host="..."))`. Confirm `--voitta-bg` resolves to the colour you expect; confirm the selector you're trying to hit is in the output.

---

## How to file a useful theming issue

Open an issue on the [Voitta Bookmarklet repo](https://github.com/voitta-ai/voitta-bookmarklet/issues) with:

1. **Plugin & host** — which plugin's theme were you applying, on which hostname.
2. **The element's tag + class list.** Open DevTools, copy the element's outer HTML up to (but not including) its first child. Indicate whether it's inside a shadow root (see step 2 above).
3. **What's wrong.** "Should follow `--voitta-bg` but stays white."
4. **A minimal `build(ctx)`** reproducer:
   ```python
   def build(ctx):
       import panel as pn, pandas as pd
       pn.extension("tabulator")
       df = pd.DataFrame({"a": [1, 2, 3]})
       return ctx.apply_theme(
           pn.Column(pn.widgets.Tabulator(df)),
           host="enterprise.voitta.ai",
       )
   ```
5. **Inspector screenshot.** Computed pane is most useful — shows the entire cascade in one image.

What you should NOT do:
- Write extensive selector overrides in `ctx.add_css` across many reports — that pattern bit-rots and obscures the real bug. A one-off override for ONE report is fine; copy-pasting the same override into ten reports is a sign the surface should be in `build_theme_css`.
- Inline `style="background:..."` attributes on Markdown / Card panes "because nothing else works" — that papers over the bug and breaks `--voitta-bg` overrides downstream.

The theming model is intentionally narrow: **one CSS string, two channels, no hidden state.** If you're hitting friction, that's where we want to fix it — usually by widening the selector coverage in `panel_app.build_theme_css` or by adding a widget class to `_shadow_dom_widget_classes()`.
