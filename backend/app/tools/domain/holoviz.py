"""HoloViz Panel report tools.

The chat pane has a left-side **report slot** that can host a Panel app
in an iframe. This module exposes the LLM-facing tool that opens it and
the read-only ``get_report_render_errors`` tool that reads the
render-error log.

Architecture:

  • Backend mounts a Panel app at ``/panel/reports`` (see
    ``app.services.panel_app`` + ``add_applications`` in ``main.py``).
    Each browser session connects via websocket; ``id`` and ``editable``
    are read from the URL query string.
  • ``show_holoviz_report`` mints a ``render_id``, embeds it in the
    iframe URL, fires the ``show_report`` browser primitive (which
    mounts the iframe in the parent ChatPane), and then *awaits* a
    ready/error signal from the iframe shim. The shim postMessages
    each event up to the parent, which POSTs it to
    ``/api/report-render-events`` — that's what wakes the await.
  • If a ``ready`` arrives within the timeout, the tool returns
    ``status="ready"``. If errors arrive, ``status="errored"`` plus the
    captured ``errors[]``. If neither arrives in time, ``status="timeout"``
    (rare; usually means the iframe never loaded).
  • Errors keep arriving after the await returns (e.g. user resizes a
    DataFrame mid-session) — the LLM can pull them later via
    ``get_report_render_errors``.

If ``id`` doesn't resolve to a stored report script, the mock layout
in ``panel_renderer.mock_layout`` is served as a fallback.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from app.services import render_events
from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# How long show_holoviz_report waits for a ready/error signal from the
# iframe shim. Long enough for cold Bokeh init + EditableTemplate hydrate
# even on a slow browser; short enough that a hung iframe doesn't stall
# the chat. 8s matches the shim's own ready-detection long-tail fallback.
DEFAULT_RENDER_WAIT_S = 8.0


def _summarise_event(ev: render_events.RenderEvent) -> dict[str, Any]:
    """Trim a RenderEvent for inclusion in the tool result. Stack and
    message are already clipped at record() time; this just drops keys
    that are noise to the LLM."""
    return {
        "kind": ev.kind,
        "ts": ev.ts,
        "source": ev.source,
        "message": ev.message,
        "stack": ev.stack,
        "url": ev.url,
        "line": ev.line,
        "col": ev.col,
    }


# ---- show_holoviz_report -------------------------------------------------


async def _show_holoviz_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return {"ok": False, "error": "report_id required"}
    title = args.get("title") or f"Report {report_id}"
    wait_s = float(args.get("wait_s") or DEFAULT_RENDER_WAIT_S)
    wait_s = max(0.5, min(30.0, wait_s))

    # Mint a render_id and start awaiting BEFORE we ask the browser to
    # mount the iframe — otherwise an instant ready signal could land
    # before begin_await() registers.
    render_id = render_events.new_render_id()
    ready = render_events.begin_await(render_id, report_id)

    # Relative to the FastAPI backend origin (the bookmarklet primitive
    # resolves it against `backendOrigin`, not the host page). The `_t`
    # cache-buster forces a fresh URL on every invocation so the iframe
    # re-navigates and Panel starts a new session — without it,
    # re-showing the same report_id keeps the existing iframe pointed at
    # the original session, which has its `build(ctx)` result cached and
    # won't pick up edits made via define_report / edit_report_script.
    # The shim reads `render_id` from the iframe URL and uses it on every
    # postMessage so the parent ChatPane forwards events tagged with it,
    # which wakes the await below.
    cache_t = int(time.time() * 1000)
    path = (
        f"/panel/reports?id={quote(report_id, safe='')}"
        f"&render_id={quote(render_id, safe='')}"
        f"&_t={cache_t}"
    )
    primitive_result = await call_browser(
        "show_report",
        {"path": path, "report_id": report_id, "title": title},
        ctx,
    )

    started = time.time()
    status = "timeout"
    saw_error = False
    try:
        # Wait for the FIRST event, then keep accumulating for a brief
        # tail (so a 'ready' that's followed 50ms later by an error
        # doesn't get reported as 'ready' alone).
        await asyncio.wait_for(ready.wait(), timeout=wait_s)
        # Drain a short tail. Errors after Bokeh fires DocumentReady are
        # the SlickGrid-after-stylesheet pattern — they happen quickly.
        tail_deadline = time.time() + 0.6
        while time.time() < tail_deadline:
            ready.clear()
            try:
                remaining = max(0.05, tail_deadline - time.time())
                await asyncio.wait_for(ready.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        _, events = render_events.collect(render_id)
        saw_error = any(e.kind == "error" for e in events)
        saw_ready = any(e.kind == "ready" for e in events)
        if saw_error:
            status = "errored"
        elif saw_ready:
            status = "ready"
        else:
            status = "unknown"
    except asyncio.TimeoutError:
        status = "timeout"
    finally:
        render_events.end_await(render_id)

    _, events = render_events.collect(render_id)
    errors = [_summarise_event(e) for e in events if e.kind == "error"]
    elapsed_ms = int((time.time() - started) * 1000)

    out: dict[str, Any] = {
        "ok": not saw_error,
        "report_id": report_id,
        "title": title,
        "path": path,
        "render_id": render_id,
        "status": status,  # "ready" | "errored" | "timeout" | "unknown"
        "elapsed_ms": elapsed_ms,
        "errors": errors,
    }
    if isinstance(primitive_result, dict):
        # Surface anything else the browser primitive returned (e.g.
        # the absolute URL it resolved to) without overwriting our
        # canonical fields.
        for k, v in primitive_result.items():
            out.setdefault(k, v)
    if saw_error:
        out["hint"] = (
            "Render-time error surfaced after the smoke test passed. "
            "Read errors[*].message and search the RAG (rag_query, "
            "corpus='docs') for the error text or 'panel <feature>' "
            "patterns. If more errors surface later, use "
            "get_report_render_errors(report_id)."
        )
    elif status == "timeout":
        out["hint"] = (
            "Iframe never reported ready or errored within the wait "
            "window. The user may not have an active chat pane open, or "
            "the iframe failed to load (check that the backend is "
            "reachable from the host page)."
        )
    return out


registry.register(
    ToolSpec(
        name="show_holoviz_report",
        description=(
            "Open a HoloViz Panel report in a left-side pane next to the "
            "chat. The pane covers the page from the left edge up to the "
            "chat drawer, with its own close (×) button.\n"
            "\n"
            "The report runs as a live Panel app served by the backend "
            "(websocket-backed Bokeh session) and embedded via <iframe>. "
            "If a stored report script matches the id, it runs; otherwise "
            "a mock layout is shown. The pane has an edit-mode toggle "
            "(⇲) that reloads the iframe with ?editable=true so the user "
            "can drag/resize/hide cards.\n"
            "\n"
            "After mounting the iframe this tool BLOCKS briefly (default "
            "8 s) waiting for the iframe to signal either 'document ready' "
            "or a render-time JS error. The result includes "
            "`status` ('ready' | 'errored' | 'timeout') and an `errors` "
            "array. If status is 'errored', read errors[*].message and "
            "search the RAG for the error text or 'panel <feature>' "
            "patterns. Fix the report and re-call this tool to verify.\n"
            "\n"
            "Errors that surface AFTER this tool returns (during user "
            "interaction) are persisted; pull them with "
            "get_report_render_errors(report_id).\n"
            "\n"
            "Only one report can be visible at a time; calling this "
            "tool again replaces the current one. The user can close the "
            "report manually with the × button."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {
                    "type": "string",
                    "description": (
                        "Slug of a stored report script (created via "
                        "define_report). If the slug doesn't match a "
                        "stored report, a mock layout is shown."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Optional pane title (default: 'Report <id>').",
                },
                "wait_s": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 30.0,
                    "default": DEFAULT_RENDER_WAIT_S,
                    "description": (
                        "Max seconds to wait for ready/error signal from "
                        "the iframe before returning. Lower this if you "
                        "want a quick mount with no synchronisation; "
                        "raise it for slow Bokeh-heavy reports."
                    ),
                },
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_show_holoviz_report,
        side="hybrid",
    )
)


# ---- verify_report --------------------------------------------------------


async def _verify_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return {"ok": False, "error": "report_id required"}
    inv_path = (
        render_events.SCRIPTS_REPORTS / report_id / "inventory.json"
    )
    if not inv_path.exists():
        return {
            "ok": False,
            "error": "no inventory recorded for this report — call "
                     "show_holoviz_report first; the iframe shim emits an "
                     "inventory snapshot right after `ready`.",
            "report_id": report_id,
        }
    try:
        import json as _json
        data = _json.loads(inv_path.read_text())
    except Exception as exc:
        return {
            "ok": False,
            "error": f"failed to read inventory: {exc}",
            "report_id": report_id,
        }
    roots = data.get("roots") if isinstance(data, dict) else None
    return {
        "ok": True,
        "report_id": report_id,
        "ts": data.get("ts"),
        "render_id": data.get("render_id"),
        "viewport": data.get("viewport"),
        "roots": roots if isinstance(roots, list) else [],
        "root_count": len(roots) if isinstance(roots, list) else 0,
    }


registry.register(
    ToolSpec(
        name="verify_report",
        description=(
            "Return a structural inventory of the most recently rendered "
            "report iframe: one entry per Bokeh root, with its model type "
            "and bounding box (x, y, width, height in pixels). The shim "
            "emits this snapshot once, right after `ready` — use it to "
            "verify 'I asked for 3 plots and got 3' without taking a "
            "screenshot.\n"
            "\n"
            "Cheaper and more reliable than screenshot_report for "
            "structural questions. Cross-origin iframes (ctx.three_scene) "
            "still show as a single root with their bounding box but the "
            "inventory cannot inspect inside them.\n"
            "\n"
            "Returns {ok, report_id, ts, render_id, viewport, roots, "
            "root_count} where roots is [{root_index, type, bbox}]. "
            "Returns ok=false when no inventory has been recorded yet "
            "(report never opened, or opened before this feature shipped)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_verify_report,
        side="server",
    )
)


# ---- get_report_render_errors --------------------------------------------


async def _get_report_render_errors(args: dict[str, Any], ctx: ToolCtx) -> Any:
    report_id = str(args.get("report_id") or "").strip()
    if not report_id:
        return {"ok": False, "error": "report_id required"}
    since_ts = args.get("since_ts")
    since_ts_f: float | None = None
    if since_ts is not None:
        try:
            since_ts_f = float(since_ts)
        except (TypeError, ValueError):
            return {"ok": False, "error": "since_ts must be a number"}
    limit = int(args.get("limit") or 50)
    limit = max(1, min(200, limit))
    entries = render_events.list_recent_for_report(
        report_id, since_ts=since_ts_f, kinds=("error",), limit=limit
    )
    return {
        "ok": True,
        "report_id": report_id,
        "count": len(entries),
        "errors": entries,
    }


registry.register(
    ToolSpec(
        name="get_report_render_errors",
        description=(
            "Read render-time JavaScript errors that an open report iframe "
            "has posted back to the backend. Use this when:\n"
            "\n"
            "  • The user says a previously-shown report is broken — the "
            "errors may have surfaced AFTER show_holoviz_report returned.\n"
            "  • You want a longer history of failures across multiple "
            "renders for the same report.\n"
            "\n"
            "Each entry includes message, stack, source, and the\n"
            "originating script URL. Filtered to error events only; defaults\n"
            "to the last 50. `source` distinguishes WHERE the error fired:\n"
            "  • 'window.error' / 'unhandledrejection' / 'console.error' /\n"
            "    'bokeh' — iframe-side JS (most common)\n"
            "  • 'server:script' — user report script raised when called\n"
            "    inside the Panel session (BEFORE the page rendered)\n"
            "  • 'server:template' — Panel/Bokeh template instantiation\n"
            "    failed (e.g. invalid layout type). These never reach\n"
            "    the iframe — without this tool the show would just\n"
            "    time out with no diagnostic.\n"
            "\n"
            "LIMIT: this tool sees errors thrown in the OUTER Bokeh "
            "document (Panel-generated JS, Bokeh internals). It does "
            "NOT see errors thrown inside sandboxed `<iframe srcdoc>` "
            "content from `ctx.three_scene` or any custom embedded "
            "iframes — those are isolated from the parent's error "
            "listener by design. For inner-iframe errors, ask the user "
            "to open DevTools and select the iframe in the console's "
            "context picker. See docs/panel-three-scene.md."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "report_id": {
                    "type": "string",
                    "description": "Slug of the report (same value passed to show_holoviz_report).",
                },
                "since_ts": {
                    "type": "number",
                    "description": (
                        "Optional unix timestamp; only return entries "
                        "newer than this. Useful for polling 'has anything "
                        "new gone wrong since I last looked?'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            "required": ["report_id"],
            "additionalProperties": False,
        },
        handler=_get_report_render_errors,
        side="server",
    )
)
