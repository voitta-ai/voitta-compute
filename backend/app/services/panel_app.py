"""Panel-served report app.

The factory is mounted into FastAPI by ``main.py`` via
``panel.io.fastapi.add_applications({"/panel/reports": panel_factory})``.
Each browser session opening that URL gets its own Bokeh document; the
factory runs once per session and returns the Panel object to display.

URL contract::

    /panel/reports?id=<report_slug>&editable=<true|false>

  * ``id`` — slug of a stored report script under
    ``scripts/reports/<slug>/code.py``. If missing or unknown, the mock
    layout from ``panel_renderer.mock_layout`` is used as a fallback.
  * ``editable`` — when ``true``, wrap the script's layout in
    ``pn.template.EditableTemplate`` so the user can drag/resize/hide
    cards. Drag commits round-trip via Bokeh comm to the live session
    (which is what ``.save()``-static HTML couldn't do).
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)


def _read_session_arg(name: str, default: str = "") -> str:
    """Pull a single string value out of ``pn.state.session_args``.

    Bokeh delivers query params as ``{name: [bytes, ...]}``. We always
    take the first value and decode UTF-8.
    """

    import panel as pn

    args = pn.state.session_args or {}
    raw = args.get(name)
    if not raw:
        return default
    first = raw[0]
    if isinstance(first, bytes):
        return first.decode("utf-8", errors="replace")
    return str(first)


_PROMOTABLE_FROM = (None, "fixed", "stretch_width", "stretch_height")


def _resolve_design(name: str):
    """Resolve a design name ('material'/'bootstrap'/'fast'/'native')
    to the Panel Design CLASS that template constructors expect.

    Panel's ``pn.config.design`` and ``Template(design=…)`` both expect
    a Design subclass (not a string). The user-facing ``ctx.set_design()``
    accepts the LLM-friendly string form; this is the resolver.
    """
    import panel as pn

    name_l = name.lower()
    # Panel's import paths: panel.theme.{material,bootstrap,fast,native}
    # — each module exports the class with the title-cased name.
    if name_l == "material":
        from panel.theme.material import Material
        return Material
    if name_l == "bootstrap":
        from panel.theme.bootstrap import Bootstrap
        return Bootstrap
    if name_l == "fast":
        from panel.theme.fast import Fast
        return Fast
    if name_l == "native":
        from panel.theme.native import Native
        return Native
    raise ValueError(f"unknown design {name!r}")


def _responsive_types_tuple() -> tuple[type, ...]:
    """Panel/widget classes whose content should fill an editable card.

    Excludes ``Markdown``/``HTML``/``Str``, all indicators, and individual
    input widgets — those should keep their natural size so titles and
    controls don't stretch into chart-shaped voids.
    """

    import panel as pn

    types: list[type] = [pn.layout.Panel]
    for name in (
        "Bokeh", "HoloViews", "Plotly", "Vega", "Matplotlib",
        "DeckGL", "ECharts", "Image",
    ):
        cls = getattr(pn.pane, name, None)
        if cls is not None:
            types.append(cls)
    for name in ("Tabulator", "DataFrame"):
        cls = getattr(pn.widgets, name, None)
        if cls is not None:
            types.append(cls)
    return tuple(types)


def _promote_sizing_mode(obj: Any, _seen: set[int] | None = None) -> None:
    """Recursively flip ``sizing_mode='stretch_both'`` on layouts and
    big-content panes/widgets so resizing an editable card actually
    reflows the chart/table inside it.

    Why we need this: Panel's EditableTemplate has a ``document_ready``
    hook that promotes only the top-level root (and even has a bug in
    the child-walking loop — see panel/template/editable/__init__.py:140).
    Reports that wrap a chart in a ``pn.Column(header_md, figure)``
    therefore don't propagate vertical resize to the inner figure: the
    Column stretches but the figure stays at its declared
    ``height=240``, leaving white space below.

    Walks three child sources:
      * ``pn.layout.Panel.objects`` (Column/Row/Tabs/GridBox children)
      * ``pn.pane.PaneBase.object`` (wrapped Bokeh figure inside
        ``pn.pane.Bokeh`` etc.)
      * Bokeh ``LayoutDOM.children`` (nested layouts in raw Bokeh)

    Only promotes when current sizing_mode is in ``_PROMOTABLE_FROM`` —
    explicit ``stretch_both``/``scale_*`` intent is preserved. Markdown,
    HTML, indicators, and input widgets are skipped via the type tuple.

    Best-effort: any exception during promotion is swallowed so a
    resize-polish hiccup never blocks a render.
    """

    import panel as pn

    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return
    _seen.add(obj_id)

    if isinstance(obj, _responsive_types_tuple()):
        sm = getattr(obj, "sizing_mode", None)
        if sm in _PROMOTABLE_FROM:
            try:
                obj.sizing_mode = "stretch_both"
            except Exception:
                pass

    children: list[Any] = []
    if isinstance(obj, pn.layout.Panel):
        children.extend(obj.objects)
    if isinstance(obj, pn.pane.PaneBase):
        wrapped = getattr(obj, "object", None)
        if wrapped is not None and hasattr(wrapped, "sizing_mode"):
            children.append(wrapped)
    raw_children = getattr(obj, "children", None)
    if isinstance(raw_children, list):
        children.extend(raw_children)

    for child in children:
        _promote_sizing_mode(child, _seen)


def _wrap_template(
    layout: Any,
    title: str,
    *,
    editable: bool,
    ctx: Any = None,
):
    """Wrap ``layout`` in a Panel template so the report iframe always
    loads our JS shim (``/api/_panel_shim.js``).

    The shim does two things that MUST run on every report show, not
    just edit mode:

      1. Captures render-time JS errors (window.error,
         unhandledrejection, console.error) and posts them up to the
         parent ChatPane → backend → awaiting ``show_holoviz_report``.
         If we only injected it in editable mode, the first show
         (always non-editable) would be invisible to the LLM — the
         exact failure mode that lets SlickGrid stylesheet errors
         slip past unreported.
      2. Signals "document ready" so the await wakes up cleanly on
         success.

    The ``ctx`` parameter is the ``ScriptContext`` from the user's
    ``build(ctx)`` call — passed in explicitly by ``panel_factory``
    (no magic layout-attribute stamping). We read:

      * ``ctx._raw_css`` → appended to ``template.config.raw_css``
      * ``ctx._js_files`` → merged into the template's ``js_files``
      * ``ctx._design`` → resolved to a Design class for the template
      * ``ctx._template_theme`` → forwarded as ``theme=``

    When ``ctx`` is ``None`` (mock layouts, error layouts), none of
    these are applied.

    Why two template classes:
      * ``editable=True`` → ``EditableTemplate`` (Muuri drag-resize +
        undo/reset toolbar buttons, hidden behind our own header).
      * ``editable=False`` → ``VanillaTemplate`` (the parent class
        ``EditableTemplate`` extends). EditableTemplate's Jinja
        unconditionally references ``roots.editor`` — which only exists
        when ``editable=True`` — so ``EditableTemplate(editable=False)``
        crashes with ``ValueError: root with 'editor' name not found``.

    If ``layout`` is a ``pn.Column`` we splat ``.objects`` into ``main``;
    anything else becomes a single main entry.
    """

    import panel as pn

    if isinstance(layout, pn.Column):
        main = list(layout.objects)
    else:
        main = [layout]

    # Merge user-supplied js_files from ctx.add_js() into our shim
    # entries. Reserved names ("voitta_panel_shim", "voitta_html2canvas")
    # win — the shim is load-bearing.
    extra_js: dict[str, str] = {}
    if ctx is not None:
        for k, v in (ctx._js_files or {}).items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                if k in ("voitta_panel_shim", "voitta_html2canvas"):
                    continue
                extra_js[k] = v

    common_kwargs: dict[str, Any] = dict(
        title=title,
        main=main,
        # Path WITHOUT leading slash: Panel prepends its own `..` + `/`
        # prefix to compute an iframe-relative URL. With `/api/...` we'd
        # get `..//api/...` which the browser resolves to `//api/...`
        # (a network-path reference) and FastAPI 404s. With `api/...` we
        # get the correct `../api/_panel_shim.js`.
        js_files={
            "voitta_panel_shim": "api/_panel_shim.js",
            # html2canvas is loaded into the iframe so the parent can
            # postMessage a screenshot request and the shim can rasterise
            # the entire report (full scrollHeight) without a headless
            # browser. Loaded in parallel with the shim itself.
            "voitta_html2canvas": "api/_html2canvas.js",
            # Plus any user-requested libs (Three.js, D3, …) from
            # ctx.add_js() — already filtered against our reserved keys.
            **extra_js,
        },
    )

    # Resolve ctx-supplied design/theme to constructor kwargs. ``design``
    # accepts a Design CLASS (Material / Bootstrap / Fast / Native);
    # ``theme`` accepts "default" or "dark" — both are read by Panel's
    # BasicTemplate constructor and apply per-template (no leak to other
    # sessions). Default None → Panel falls back to its own defaults.
    if ctx is not None:
        if ctx._design is not None:
            common_kwargs["design"] = _resolve_design(ctx._design)
        if ctx._template_theme is not None:
            common_kwargs["theme"] = ctx._template_theme

    if editable:
        template = pn.template.EditableTemplate(
            editable=True,
            local_save=False,
            **common_kwargs,
        )
    else:
        template = pn.template.VanillaTemplate(**common_kwargs)

    # Hide the template's own blue nav header — our outer ReportPane
    # surfaces the title + (when editing) edit toggle + undo/reset buttons.
    # In editable mode, the undo/reset buttons in the parent post messages
    # to this iframe; the shim installs the listener and clicks the
    # (still-rendered, just hidden) Panel buttons by id.
    template.config.raw_css.append(
        """
        #header { display: none !important; }
        #container { padding-top: 0 !important; }
        #main { margin-top: 8px !important; }

        /* Thin scrollbars so the report iframe doesn't show the fat
           macOS native chrome on dark themes. WebKit gets explicit
           track/thumb colors; Firefox uses scrollbar-* shorthand. The
           colors reference the theme tokens when a theme is applied
           (see build_theme_css below) and fall back to neutral
           translucent values that read OK on either light or dark
           defaults. */
        * {
          scrollbar-width: thin;
          scrollbar-color: var(--voitta-scrollbar-thumb, rgba(128,128,128,0.45))
                           var(--voitta-scrollbar-track, transparent);
        }
        *::-webkit-scrollbar {
          width: 10px;
          height: 10px;
        }
        *::-webkit-scrollbar-track {
          background: var(--voitta-scrollbar-track, transparent);
        }
        *::-webkit-scrollbar-thumb {
          background-color: var(--voitta-scrollbar-thumb, rgba(128,128,128,0.45));
          border-radius: 6px;
          border: 2px solid transparent;
          background-clip: padding-box;
        }
        *::-webkit-scrollbar-thumb:hover {
          background-color: var(--voitta-scrollbar-thumb-hover, rgba(128,128,128,0.7));
        }
        *::-webkit-scrollbar-corner {
          background: transparent;
        }
        """
    )

    # ctx.add_css() / ctx.apply_theme() raw CSS — routed into the
    # template's raw_css list. Lands as a real <style> block in <head>,
    # sibling to Panel's bundled stylesheets. The naive alternative
    # ``pn.pane.HTML('<style>…</style>')`` does NOT work — Panel
    # sanitises HTML panes, so rules become literal text in a <div>
    # and never reach Tabulator / Bokeh widgets.
    if ctx is not None:
        for css in (ctx._raw_css or []):
            if isinstance(css, str) and css.strip():
                template.config.raw_css.append(css)

    # NB: theme injection used to live here as a hidden post-build
    # step — we'd read ``layout._voitta_theme_tokens`` and call
    # ``build_theme_css(tokens, is_dark)`` from the wrapper. That
    # path produced the right CSS for outer-document widgets but
    # missed shadow-DOM widgets (Tabulator, Bokeh DataTable), which
    # silently kept their default light theme. The fix routes theme
    # CSS through the same channel as every other piece of report
    # CSS: ``ctx.apply_theme`` now calls ``add_css`` AND attaches
    # ``stylesheets=[…]`` to shadow-DOM widgets at build time. See
    # ScriptContext.apply_theme / theme_css for the explicit form.
    # No magic attribute reads here.
    return template


def build_theme_css(tokens: dict[str, str], is_dark: bool) -> str:
    """Assemble the report-iframe stylesheet for a stamped theme.

    Two layers:
      1. Declare every ``--voitta-…`` token on ``:root`` so the cascade
         makes them available to user CSS, ``pn.pane.HTML`` content,
         and any selector that reaches for ``var(--voitta-…)``.
      2. A set of surface-targeting rules using those vars — covers the
         common Panel / Bokeh DOM nodes that would otherwise stay in
         their default light theme (white #main, light Markdown text,
         white Tabulator cells, white input chrome).

    Selectors are kept defensive — Panel changes Bokeh class names
    between minor versions. Where two selectors might both match a
    surface, we list both rather than rely on internal stability.
    """
    var_block = "\n".join(
        f"  {name}: {value};"
        for name, value in sorted(tokens.items())
        if name.startswith("--voitta-")
    )
    # Bridge our ``--voitta-*`` namespace into Panel's ``--panel-*``
    # namespace. Panel's bundled ``native.css`` sets
    #   body { color: var(--background-text-color); }
    # where ``--background-text-color`` resolves through a fallback
    # chain that ends at ``var(--panel-on-background-color)``.  Panel
    # expects the host page (JupyterLab, VSCode, pydata-sphinx, …) to
    # define ``--panel-*`` tokens.  In our iframe nobody does — so the
    # chain falls through to the user-agent default of black-on-white
    # and Markdown text renders as black-on-dark in dark themes.
    #
    # The bridge below maps our four big surface tokens onto Panel's,
    # which routes every Panel-bundled stylesheet (markdown.css and
    # friends) through OUR colour palette automatically. Fixes Markdown
    # bodies + any future pane that consumes the ``--panel-*`` chain
    # without us needing to chase Panel's selector list.
    panel_bridge = (
        "  --panel-background-color: var(--voitta-bg);\n"
        "  --panel-on-background-color: var(--voitta-text);\n"
        "  --panel-surface-color: var(--voitta-surface);\n"
        "  --panel-on-surface-color: var(--voitta-text);\n"
    )
    var_block = var_block + "\n" + panel_bridge.rstrip()
    # The variable block uses both ``:root`` (outer document) AND
    # ``:host`` (Bokeh widget shadow roots). Same CSS string is
    # injected into both surfaces:
    #   * ``ctx.add_css(css)`` → ``template.config.raw_css`` → outer doc
    #     ``<head>`` — reaches non-shadow widgets (Markdown, Card,
    #     plain panes).
    #   * ``stylesheets=[css]`` on a Bokeh widget → widget's shadow
    #     root — reaches widgets like Tabulator and Bokeh DataTable
    #     whose chrome lives behind a shadow boundary that outer-doc
    #     CSS can't pierce.
    # Using ``:root, :host`` makes the same string work in both
    # contexts. Without the ``:host`` half, our theme tokens stop at
    # the shadow boundary and Tabulator falls back to its default
    # light theme.
    return f""":root, :host {{
{var_block}
  color-scheme: {"dark" if is_dark else "light"};
}}

/* Outer document — fills the iframe behind everything */
html, body {{
  background-color: var(--voitta-bg) !important;
  color: var(--voitta-text);
  font-family: var(--voitta-font-sans, system-ui, sans-serif);
}}

/* Vanilla / Editable templates' main scroll container */
#container, #main, #content {{
  background-color: var(--voitta-bg) !important;
  color: var(--voitta-text);
}}

/* Bokeh / Panel rendering shells — both classic and post-3.x class names */
.bk-Panel, .bk-Column, .bk-Row, .bk-clearfix {{
  color: var(--voitta-text);
}}

/* Markdown panes carry the bulk of report prose. Bokeh wraps the
   actual <h1>/<p>/<a> in a Shadow-DOM-free <div> so simple selectors
   reach in. */
.bk-Markdown, .markdown-body {{
  color: var(--voitta-text);
  background-color: transparent;
}}
.bk-Markdown h1, .bk-Markdown h2, .bk-Markdown h3,
.bk-Markdown h4, .bk-Markdown h5, .bk-Markdown h6,
.markdown-body h1, .markdown-body h2, .markdown-body h3,
.markdown-body h4, .markdown-body h5, .markdown-body h6 {{
  color: var(--voitta-text);
  border-color: var(--voitta-divider);
}}
.bk-Markdown p, .bk-Markdown li, .bk-Markdown span,
.markdown-body p, .markdown-body li, .markdown-body span {{
  color: var(--voitta-text);
}}
.bk-Markdown a, .markdown-body a {{
  color: var(--voitta-link-fg, var(--voitta-accent));
}}
.bk-Markdown code, .markdown-body code {{
  background-color: var(--voitta-code-bg);
  color: var(--voitta-text);
}}
.bk-Markdown pre, .markdown-body pre {{
  background-color: var(--voitta-code-block-bg);
  color: var(--voitta-code-block-fg);
}}
.bk-Markdown blockquote, .markdown-body blockquote {{
  color: var(--voitta-text-muted);
  border-left-color: var(--voitta-accent);
}}
.bk-Markdown hr, .markdown-body hr {{
  border-color: var(--voitta-divider);
}}
.bk-Markdown table th, .bk-Markdown table td,
.markdown-body table th, .markdown-body table td {{
  border-color: var(--voitta-border);
}}
.bk-Markdown table th, .markdown-body table th {{
  background-color: var(--voitta-surface);
}}

/* Card containers used by GridSpec / EditableTemplate */
.bk-Card, .pn-card {{
  background-color: var(--voitta-surface);
  border-color: var(--voitta-border);
  color: var(--voitta-text);
}}
.bk-Card-header, .pn-card-header {{
  background-color: var(--voitta-surface);
  color: var(--voitta-text);
  border-color: var(--voitta-divider);
}}

/* Tabulator tables (pn.widgets.Tabulator)
 *
 * Tabulator's bundled tabulator_simple.min.css loads INTO the same
 * shadow root we inject into and includes hard-coded white
 * backgrounds via the selector ``.tabulator-row,
 * .tabulator-row.tabulator-row-even`` (background: #fff).
 * It uses class-pair selectors (specificity 0-2-0) and gets a
 * later-cascade-position win on equal specificity. We can't win on
 * order (Bokeh appends Tabulator's CSS after ours) so we win on
 * !important — every Tabulator rule below is force-applied. This
 * also covers cell-level borders that Tabulator paints over our
 * row-level border-color.
 *
 * Even-row stripe: Tabulator uses an explicit class
 * (.tabulator-row.tabulator-row-even), NOT :nth-child(even) — the
 * latter doesn't fire because Tabulator's virtualised rows aren't
 * siblings of the same parent. */
.tabulator, .tabulator-tableholder {{
  background-color: var(--voitta-surface) !important;
  color: var(--voitta-text) !important;
  border-color: var(--voitta-border) !important;
}}
.tabulator-header,
.tabulator-col,
.tabulator-col-content {{
  background-color: var(--voitta-surface) !important;
  color: var(--voitta-text) !important;
  border-color: var(--voitta-border) !important;
}}
.tabulator-row,
.tabulator-row.tabulator-row-odd {{
  background-color: var(--voitta-surface) !important;
  color: var(--voitta-text) !important;
  border-color: var(--voitta-divider) !important;
}}
.tabulator-row.tabulator-row-even {{
  background-color: var(--voitta-bg) !important;
}}
/* Cell is the actual text container — without an explicit colour
 * here, Tabulator's bundled per-cell ``color:#333`` wins and rows
 * stay near-invisible on a dark theme even after the row bg is fixed. */
.tabulator-cell {{
  color: var(--voitta-text) !important;
  border-color: var(--voitta-divider) !important;
}}
/* Hover: Tabulator scopes its own hover to .tabulator-selectable;
 * matching that selector means we override cleanly without
 * accidentally restyling non-interactive rows. */
.tabulator-row.tabulator-selectable:hover,
.tabulator-row:hover {{
  background-color: var(--voitta-art-row-hover, var(--voitta-divider)) !important;
}}
/* Footer / pagination chrome. */
.tabulator-footer,
.tabulator-paginator,
.tabulator-page {{
  background-color: var(--voitta-surface) !important;
  color: var(--voitta-text) !important;
  border-color: var(--voitta-border) !important;
}}
.tabulator-page.active {{
  background-color: var(--voitta-accent) !important;
  color: var(--voitta-accent-fg, var(--voitta-bg)) !important;
}}
.tabulator-footer {{
  background-color: var(--voitta-surface);
  color: var(--voitta-text-muted);
  border-color: var(--voitta-border);
}}

/* Input widgets — date pickers, select dropdowns, textareas */
input, textarea, select,
.bk-input, .pn-input {{
  background-color: var(--voitta-bg);
  color: var(--voitta-text);
  border-color: var(--voitta-border);
}}
input:focus, textarea:focus, select:focus,
.bk-input:focus, .pn-input:focus {{
  border-color: var(--voitta-accent);
  outline: 2px solid var(--voitta-accent-tint, transparent);
  outline-offset: -1px;
}}

/* Buttons (pn.widgets.Button) */
button.bk-btn, .pn-button {{
  background-color: var(--voitta-surface);
  color: var(--voitta-text);
  border-color: var(--voitta-border);
}}
button.bk-btn:hover, .pn-button:hover {{
  background-color: var(--voitta-accent-tint, var(--voitta-surface));
}}
button.bk-btn.bk-btn-primary {{
  background-color: var(--voitta-accent);
  color: var(--voitta-accent-fg);
  border-color: var(--voitta-accent);
}}

/* Tabs (pn.Tabs) */
.bk-Tabs .bk-tab {{
  background-color: var(--voitta-surface);
  color: var(--voitta-text-muted);
  border-color: var(--voitta-border);
}}
.bk-Tabs .bk-tab.bk-active {{
  background-color: var(--voitta-bg);
  color: var(--voitta-text);
  border-bottom-color: var(--voitta-accent);
}}

/* Plotly defaults to white modebar background — wash it out */
.modebar {{
  background-color: transparent !important;
}}
.modebar-btn path {{
  fill: var(--voitta-text-muted) !important;
}}
.modebar-btn:hover path {{
  fill: var(--voitta-text) !important;
}}
"""


def _record_server_error(
    render_events_mod,
    render_id: str,
    report_id: str,
    exc: BaseException,
    *,
    source: str,
) -> None:
    """Push a server-side render exception into the render_events store.

    Mirrors the iframe-shim path so ``show_holoviz_report`` and
    ``get_report_render_errors`` surface failures regardless of where
    they happened. ``render_id`` may be empty in the rare case a report
    is opened directly in a browser tab without an awaiting tool — the
    persistent per-report log still captures it for a later
    ``get_report_render_errors`` call.
    """
    import traceback

    msg = f"{type(exc).__name__}: {exc}"
    try:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        stack = None
    try:
        render_events_mod.record(
            render_id=render_id or "server-direct",
            report_id=report_id or "(unknown)",
            kind="error",
            message=msg,
            stack=stack,
            source=source,
        )
    except Exception:
        # Recording must never crash the request handler — that's the
        # whole point of this helper.
        log.exception("render_events.record failed (best-effort)")


def _error_layout(message: str):
    """Inline error block when the script fails — keeps the page from
    falling back to a Bokeh 500."""

    import panel as pn

    return pn.Column(
        pn.pane.Markdown(
            "## ⚠️ Report failed\n\n"
            "The report script raised an exception. Details below.",
            sizing_mode="stretch_width",
        ),
        pn.pane.Markdown(
            f"```\n{message}\n```",
            sizing_mode="stretch_width",
        ),
        sizing_mode="stretch_width",
        margin=(20, 24),
    )


def panel_factory():
    """Build the Panel object for a single browser session.

    Called by ``panel.io.fastapi`` once per session connection; we read
    session-scoped query params and dispatch to either the user's stored
    report script or the mock fallback.

    Two layers of error capture funnel through ``render_events.record``
    so the LLM-facing ``show_holoviz_report`` / ``get_report_render_errors``
    tools see *every* failure path, not just iframe-side JS:

      1. ``ScriptError`` from ``report_script_layout`` — user script body
         raised. Same path we've always had; the resulting error layout
         is wrapped normally and the shim captures the failure too.
      2. Anything from ``_wrap_template`` — Panel parameter validation,
         template instantiation, theme-CSS assembly. These fire *after*
         the user script returned cleanly, so prior versions of this
         function let them bubble straight out of ``panel_factory``,
         which makes Panel render its bare Bokeh 500 page in the iframe
         with no shim injected → the awaiting tool times out with an
         empty ``errors[]`` and the LLM is flying blind.

    Server-side records carry ``source="server"`` so the LLM can tell
    them apart from iframe-side ``window.error`` reports.
    """

    # Lazy imports so FastAPI startup doesn't pay the Panel/Bokeh cost.
    from app.services import panel_renderer, render_events
    from app.services.scripts import ScriptError, report_script_layout

    report_id = _read_session_arg("id", default="")
    editable = _read_session_arg("editable", default="false").lower() == "true"
    render_id = _read_session_arg("render_id", default="")
    title = f"Report {report_id}" if report_id else "Report"

    layout: Any = None
    ctx: Any = None
    try:
        if report_id:
            result = report_script_layout(report_id)
            if result is not None:
                layout, ctx = result
    except ScriptError as exc:
        log.warning("report script %r failed: %s", report_id, exc)
        _record_server_error(render_events, render_id, report_id, exc, source="server:script")
        layout = _error_layout(str(exc))
        # ctx stays None — error layouts don't carry user-supplied
        # raw_css / js_files / design / theme.

    if layout is None:
        layout = panel_renderer.mock_layout(report_id or "(unspecified)")

    # Always wrap — the template injects the shim that captures
    # render-time errors and signals ready. ``editable`` controls
    # whether Muuri activates drag/resize. ``ctx`` carries
    # ctx.add_css / ctx.add_js / ctx.set_design / ctx.set_template_theme
    # explicitly (no magic layout-attribute reads).
    try:
        return _wrap_template(layout, title, editable=editable, ctx=ctx)
    except Exception as exc:
        log.warning(
            "_wrap_template raised for report %r: %s", report_id, exc,
        )
        _record_server_error(
            render_events, render_id, report_id, exc, source="server:template",
        )
        try:
            return _wrap_template(
                _error_layout(f"{type(exc).__name__}: {exc}"),
                title,
                editable=False,
            )
        except Exception:
            # If even the error-layout wrap fails, fall back to a bare
            # Column so Panel still has SOMETHING to render. Better a
            # plain markdown traceback than the Bokeh 500.
            log.exception("error-layout wrap also failed for %r", report_id)
            return _error_layout(f"{type(exc).__name__}: {exc}")
