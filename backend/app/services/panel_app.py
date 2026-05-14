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


def _wrap_template(layout: Any, title: str, *, editable: bool):
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

    Why two template classes:
      * ``editable=True`` → ``EditableTemplate`` (Muuri drag-resize +
        undo/reset toolbar buttons, hidden behind our own header).
      * ``editable=False`` → ``VanillaTemplate`` (the parent class
        ``EditableTemplate`` extends). EditableTemplate's Jinja
        unconditionally references ``roots.editor`` — which only exists
        when ``editable=True`` — so ``EditableTemplate(editable=False)``
        crashes with ``ValueError: root with 'editor' name not found``.
        VanillaTemplate is the lightest template that still threads
        ``js_files`` through the same resource pipeline.

    Both templates accept ``js_files`` via ``_base_config.param.update``
    (see panel/template/base.py:127–130 — kwargs matching ``_base_config``
    params get applied to ``self.config``).

    If ``layout`` is a ``pn.Column`` we splat ``.objects`` into ``main``;
    anything else becomes a single main entry.
    """

    import panel as pn

    if isinstance(layout, pn.Column):
        main = list(layout.objects)
    else:
        main = [layout]

    # In editable mode, propagate stretch_both deep into each card so
    # resizing actually reflows the inner chart/table — Panel's own
    # ready hook only flips the top-level root. See _promote_sizing_mode.
    if editable:
        for item in main:
            _promote_sizing_mode(item)

    # Report scripts can request extra <script src=...> entries via
    # ``ctx.add_js(name, url)``; report_script_layout forwards them onto
    # ``layout._voitta_extra_js_files``. We merge them into the template's
    # ``js_files`` kwarg so they end up in the iframe's <head>. Script
    # names must not collide with voitta's own entries below — we win.
    extra_js: dict[str, str] = {}
    raw_extra = getattr(layout, "_voitta_extra_js_files", None)
    if isinstance(raw_extra, dict):
        for k, v in raw_extra.items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                if k in ("voitta_panel_shim", "voitta_html2canvas"):
                    continue  # don't let scripts shadow our shim
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
        """
    )
    return template


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
    """

    # Lazy imports so FastAPI startup doesn't pay the Panel/Bokeh cost.
    from app.services import panel_renderer
    from app.services.scripts import ScriptError, report_script_layout

    report_id = _read_session_arg("id", default="")
    editable = _read_session_arg("editable", default="false").lower() == "true"
    title = f"Report {report_id}" if report_id else "Report"

    try:
        layout = report_script_layout(report_id) if report_id else None
    except ScriptError as exc:
        log.warning("report script %r failed: %s", report_id, exc)
        layout = _error_layout(str(exc))

    if layout is None:
        layout = panel_renderer.mock_layout(report_id or "(unspecified)")

    # Always wrap — the template injects the shim that captures
    # render-time errors and signals ready. ``editable`` controls
    # whether Muuri activates drag/resize.
    return _wrap_template(layout, title, editable=editable)
