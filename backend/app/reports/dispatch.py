"""Post-build dispatcher — single HTML path.

After ``sandbox.run()`` returns:

  1. Emit any inline items the script accumulated on ``ctx`` as
     Chainlit messages.
  2. Render the build() return value as HTML (via
     ``app.reports.renderers.html.render_html``), cache it, get
     back an iframe URL.
  3. Tell the FE to mount that URL via the ``show_html_report``
     call_fn. Await the render-event drain.

There is no kind detection. There is no other renderer. If the
script returns nothing (or only emits inline content), the
dispatcher returns ``status="no-render"`` and the chat still
shows whatever inline items the script produced.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import chainlit as cl

from app.reports import render_events, store
from app.reports.renderers.html import render_html
from app.reports.schemas import HtmlPayload

logger = logging.getLogger(__name__)


def _cl_context_active() -> bool:
    """True when called inside an active Chainlit session (chat context)."""
    try:
        from chainlit.context import context as _ctx
        _ = _ctx.session
        return True
    except Exception:
        return False


@dataclass
class DispatchResult:
    ok: bool
    kind: Optional[str] = None
    status: str = "ok"          # "ready" | "errored" | "timeout" | "no-render" | "error"
    elapsed_ms: int = 0
    errors: list[dict[str, Any]] | None = None
    inventory: dict[str, Any] | None = None
    error: Optional[str] = None
    inline: list[dict[str, Any]] | None = None


async def emit_inline(ctx) -> None:
    """Emit the script's inline items as Chainlit messages."""
    for item in ctx.inline:
        if item.kind == "text":
            await cl.Message(content=item.payload["body"]).send()
        elif item.kind == "image":
            raw = base64.b64decode(item.payload["data"])
            await cl.Message(
                content=item.payload.get("alt") or "",
                elements=[cl.Image(
                    name="inline",
                    content=raw,
                    mime=item.payload["mime"],
                )],
            ).send()
        elif item.kind == "json":
            import json
            await cl.Message(
                content="```json\n" + json.dumps(item.payload["value"], indent=2) + "\n```"
            ).send()


async def ship_html_report(
    slug: str,
    payload: HtmlPayload,
    *,
    title: Optional[str],
    render_id: str,
    wait_s: float,
) -> DispatchResult:
    """Tell the FE to mount the HTML iframe and await render-events."""
    args_payload = {
        "name": slug,
        "title": title,
        "render_id": render_id,
        "url": payload.url,
        "kind": "html",
    }
    t0 = time.perf_counter()
    try:
        await cl.CopilotFunction(name="show_html_report", args=args_payload).acall()
    except Exception as exc:
        logger.exception("show_html_report call_fn failed")
        return DispatchResult(
            ok=False, status="error", error=f"call_fn failed: {exc}", kind="html",
        )

    event = await render_events.wait_for(slug, timeout=wait_s)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if event is None:
        return DispatchResult(
            ok=False, status="timeout", elapsed_ms=elapsed_ms, kind="html",
            error=f"no render-event after {wait_s:.1f}s",
        )
    if event.kind == "error":
        return DispatchResult(
            ok=False, status="errored", elapsed_ms=elapsed_ms, kind="html",
            errors=[{"message": event.message, "detail": event.detail}],
        )
    return DispatchResult(ok=True, status="ready", elapsed_ms=elapsed_ms, kind="html")


async def run_and_dispatch(
    slug: str,
    *,
    args: Optional[dict[str, Any]] = None,
    title: Optional[str] = None,
    wait_s: float = 8.0,
    host: Optional[str] = None,
) -> DispatchResult:
    """Full pipeline: read source → sandbox.run → inline emit → render → ship."""
    from app.reports import sandbox

    if not store.exists(slug):
        return DispatchResult(ok=False, status="error", error=f"script {slug!r} not found")

    code = store.read_code(slug)
    run = await sandbox.run(slug, code, args=args, host=host)
    if not run.ok:
        store.update_meta(slug, last_ok=False, last_run_at=_now_iso())
        return DispatchResult(
            ok=False, status="error", error=run.error,
            errors=[{"message": run.error or "", "detail": {"traceback": run.traceback}}],
        )

    assert run.ctx is not None
    in_chat = _cl_context_active()

    inline_items = [{"kind": i.kind, "payload": i.payload} for i in run.ctx.inline]

    if in_chat:
        await emit_inline(run.ctx)

    if run.result is None:
        store.update_meta(slug, last_ok=True, last_run_at=_now_iso(), last_kind=None)
        return DispatchResult(ok=True, status="no-render", inline=inline_items or None)

    render_id = uuid.uuid4().hex
    try:
        payload = render_html(run.result, slug=slug, render_id=render_id)
    except (TypeError, ValueError) as exc:
        store.update_meta(slug, last_ok=False, last_run_at=_now_iso())
        return DispatchResult(
            ok=False, status="error", error=str(exc),
            errors=[{"message": str(exc), "detail": {
                "returned_type": type(run.result).__name__,
            }}],
        )

    if in_chat:
        # Chat path: push report to the open ReportPane via Chainlit call_fn.
        result = await ship_html_report(
            slug, payload, title=title, render_id=render_id, wait_s=wait_s,
        )
    else:
        # REST path: report is rendered and cached; return the URL so the
        # caller can open it directly (no Chainlit session available).
        result = DispatchResult(
            ok=True, status="ready", kind="html",
            inventory={"url": payload.url, "render_id": render_id},
            inline=inline_items or None,
        )

    store.update_meta(
        slug, last_ok=result.ok, last_run_at=_now_iso(), last_kind="html",
    )
    return result


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
