"""CLI back-channel — REST endpoints for external automation.

NOT exposed to the in-pane chat LLM. Designed for use by Claude Code /
curl / Python scripts running on the same host as the backend, to
inspect and drive whichever pages already have the bookmarklet attached.

Localhost-only. The routes refuse:

  • Non-loopback peer addresses (anything other than ``127.0.0.1`` /
    ``::1``). The backend already binds to 127.0.0.1 in production, but
    this stays correct if HOST is ever reconfigured.
  • Browser-originated requests (``Origin`` header set). Real CLI
    clients don't send Origin; a tab in the user's browser does. The
    project's CORS config is permissive (``allow_origins=["*"]``), so
    without this check a malicious page could CSRF the eval endpoint
    and run arbitrary JS in any other page that has the bookmarklet
    running. Refusing Origin removes that vector.

The companion browser primitives (``get_page_dump``, ``eval_js``) live
in ``frontend/src/lib/primitives.ts``. They are deliberately not
registered as ``ToolSpec``s, so the chat LLM cannot invoke them —
only this REST layer can.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.bridge import ToolBridgeError, bridge


router = APIRouter(prefix="/cli", tags=["cli"])


# ---- access guard ---------------------------------------------------------


_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def _localhost_only(request: Request) -> None:
    """Reject anything not from loopback or that smells like a browser."""
    peer = (request.client.host if request.client else "") or ""
    if peer not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail=f"/cli endpoints accept loopback only (peer={peer!r})",
        )
    if request.headers.get("origin"):
        # A CLI client (curl, httpx, requests) never sets Origin. A
        # cross-origin browser request always does. If the header is
        # present, this isn't our intended consumer.
        raise HTTPException(
            status_code=403,
            detail="/cli rejects browser Origin (use a CLI tool, not a tab)",
        )


# ---- /cli — help / docstring ----------------------------------------------


@router.get("", include_in_schema=True)
async def cli_help(_: None = Depends(_localhost_only)) -> dict:
    """Self-describing manifest. The same content is returned at /cli/help.

    Designed for an LLM agent to GET once at startup so it knows which
    endpoints exist, what each takes/returns, and how to chain them.
    """
    return _MANIFEST


@router.get("/help")
async def cli_help_alias(_: None = Depends(_localhost_only)) -> dict:
    return _MANIFEST


# ---- /cli/sessions --------------------------------------------------------


@router.get("/sessions")
async def cli_sessions(_: None = Depends(_localhost_only)) -> dict:
    """List every bridge session the backend currently knows about.

    A session is one tab with the bookmarklet running. Use the returned
    ``session_id`` as the ``session_id`` argument to ``/cli/page`` and
    ``/cli/eval``. Each entry includes ``connected`` (False = the SSE
    inbox is currently dropped, the tab may be unloaded), the page's
    URL/host/title, the user agent, and timestamps.
    """
    sessions = bridge.list_sessions()
    # Surface a flatter view of the page block so the LLM consumer
    # doesn't have to reach through `identification.page`. Keep the
    # full record under `raw` for completeness.
    flat: list[dict[str, Any]] = []
    for s in sessions:
        ident = s.get("identification") or {}
        page = ident.get("page") or {}
        flat.append(
            {
                "session_id": s.get("session_id"),
                "connected": s.get("connected"),
                "inbox_streams": s.get("inbox_streams"),
                "url": page.get("href"),
                "host": page.get("host"),
                "pathname": page.get("pathname"),
                "title": page.get("title"),
                "user_agent": ident.get("user_agent"),
                "capabilities": s.get("capabilities") or [],
                "pending_calls": s.get("pending_calls") or [],
                "created_at": s.get("created_at"),
                "last_seen": s.get("last_seen"),
                "raw": s,
            }
        )
    return {"count": len(flat), "sessions": flat}


# ---- /cli/page ------------------------------------------------------------


@router.get("/page")
async def cli_page(
    session_id: str = Query(..., min_length=8, max_length=128),
    timeout_ms: int = Query(15_000, ge=500, le=120_000),
    _: None = Depends(_localhost_only),
) -> dict:
    """Return URL + title + full ``<html>`` outerHTML for a session.

    Single round-trip via the ``get_page_dump`` browser primitive. No
    size cap on the returned HTML — Claude Code is presumed to handle
    large bodies. If the bookmarklet tab is suspended, you'll get an
    HTTP 502 with ``error: 'no_session'``.
    """
    try:
        result = await bridge.call(
            session_id, "get_page_dump", {}, timeout_ms=timeout_ms
        )
    except ToolBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": exc.kind, "message": str(exc), "session_id": session_id},
        )
    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "primitive_failed",
                "session_id": session_id,
                "primitive_error": result.error,
            },
        )
    page = result.result if isinstance(result.result, dict) else {}
    return {
        "ok": True,
        "session_id": session_id,
        "url": page.get("url"),
        "title": page.get("title"),
        "pathname": page.get("pathname"),
        "search": page.get("search"),
        "hash": page.get("hash"),
        "user_agent": page.get("user_agent"),
        "html": page.get("html"),
        "html_bytes": len(page.get("html") or ""),
        "fetched_at_ms": page.get("ts"),
        "latency_ms": result.latency_ms,
    }


# ---- /cli/eval ------------------------------------------------------------


class EvalRequest(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=128)
    js: str = Field(
        ...,
        min_length=1,
        description=(
            "JavaScript source. Wrapped in an async function in the "
            "browser, so you can `await` and `return` directly. The "
            "function's return value is what comes back as `value`."
        ),
    )
    await_ms: int | None = Field(
        default=None,
        description=(
            "How long the in-browser awaiter waits for your code to "
            "settle (default 30_000ms). Hard cap on async work."
        ),
    )
    timeout_ms: int | None = Field(
        default=None,
        description=(
            "How long the backend waits for the bridge round-trip "
            "(default = await_ms + 5_000ms). Increase only if your "
            "code legitimately runs longer than that."
        ),
    )


@router.post("/eval")
async def cli_eval(
    req: EvalRequest, _: None = Depends(_localhost_only)
) -> dict:
    """Run arbitrary JavaScript in the bookmarklet's tab.

    The source string is wrapped in an ``AsyncFunction`` body, so:

      * ``await`` is allowed at the top level.
      * Use ``return X`` to send a value back. The value is serialised
        with a custom replacer that handles DOM nodes, Errors, Map/Set,
        BigInt, Symbol, undefined, NaN/Infinity, cycles, etc., wrapped
        as ``{__type, ...}``.

    Console output (`console.log/info/warn/error/debug`) is captured
    for the duration of the eval and returned alongside the value.
    Throws are reported as ``{ok: false, error: {name, message, stack}}``
    — they don't 500.
    """
    await_ms = req.await_ms if req.await_ms and req.await_ms > 0 else 30_000
    timeout_ms = (
        req.timeout_ms
        if req.timeout_ms and req.timeout_ms > 0
        else await_ms + 5_000
    )
    try:
        result = await bridge.call(
            req.session_id,
            "eval_js",
            {"js": req.js, "await_ms": await_ms},
            timeout_ms=timeout_ms,
        )
    except ToolBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": exc.kind,
                "message": str(exc),
                "session_id": req.session_id,
            },
        )
    if not result.ok:
        # The primitive itself failed to dispatch (e.g. bad js arg).
        return {
            "ok": False,
            "session_id": req.session_id,
            "primitive_error": result.error,
            "latency_ms": result.latency_ms,
        }
    payload = result.result if isinstance(result.result, dict) else {}
    return {
        **payload,
        "session_id": req.session_id,
        "latency_ms": result.latency_ms,
    }


# ---- self-describing manifest --------------------------------------------


_MANIFEST: dict[str, Any] = {
    "title": "VOITTA bookmarklet — CLI back-channel",
    "summary": (
        "Localhost-only REST endpoints for driving any browser tab that "
        "currently has the bookmarklet attached. Intended for use by "
        "Claude Code or other local automation; the in-pane chat LLM "
        "cannot reach these. Use `/cli/sessions` to discover targets, "
        "then call `/cli/page` or `/cli/eval` with the session_id."
    ),
    "access": {
        "scheme": "https",
        "host": "127.0.0.1",
        "port_default": 12358,
        "auth": "none",
        "rules": [
            "Loopback peer only (127.0.0.1 / ::1).",
            "Origin header must be absent (browser tabs rejected).",
        ],
    },
    "endpoints": [
        {
            "method": "GET",
            "path": "/cli",
            "alias": "/cli/help",
            "description": "This manifest.",
            "params": {},
            "returns": "manifest object",
        },
        {
            "method": "GET",
            "path": "/cli/sessions",
            "description": (
                "List every bookmarklet session the backend knows. "
                "Use to find session_ids for /cli/page and /cli/eval."
            ),
            "params": {},
            "returns": {
                "count": "int",
                "sessions": (
                    "list of {session_id, connected, url, host, "
                    "pathname, title, user_agent, username, "
                    "capabilities, pending_calls, created_at, "
                    "last_seen, raw}"
                ),
            },
            "notes": [
                "connected=false means the tab is suspended or closed.",
                "Multiple sessions are normal (one per bookmarked tab).",
            ],
        },
        {
            "method": "GET",
            "path": "/cli/page",
            "description": (
                "Return URL + title + full document.documentElement."
                "outerHTML for one session."
            ),
            "params": {
                "session_id": "string, required (from /cli/sessions)",
                "timeout_ms": "int, optional (default 15000, max 120000)",
            },
            "returns": {
                "ok": "true",
                "url": "string",
                "title": "string",
                "pathname": "string",
                "search": "string (incl. leading ?)",
                "hash": "string (incl. leading #)",
                "user_agent": "string",
                "html": "string — full page outerHTML, no cap",
                "html_bytes": "int",
                "fetched_at_ms": "int (browser Date.now)",
                "latency_ms": "int (round-trip)",
            },
            "notes": [
                "No size cap on `html`. Claude Code handles large "
                "responses; this is intentional.",
                "If the tab is suspended, returns HTTP 502 with "
                "{error: 'no_session'}.",
            ],
        },
        {
            "method": "POST",
            "path": "/cli/eval",
            "description": (
                "Run arbitrary JavaScript in the page. The code body "
                "is wrapped in an AsyncFunction, so top-level await "
                "and `return X` work."
            ),
            "body": {
                "session_id": "string, required",
                "js": "string, required — JS source",
                "await_ms": "int, optional (default 30000) — in-browser timeout",
                "timeout_ms": (
                    "int, optional (default await_ms+5000) — bridge "
                    "round-trip timeout"
                ),
            },
            "returns": {
                "ok": "bool — false if the user code threw or timed out",
                "value": (
                    "JSON-safe encoding of whatever the wrapped function "
                    "returned. Non-JSON values (DOM nodes, Map/Set, "
                    "BigInt, Errors, undefined, NaN, cycles, ...) are "
                    "wrapped as {__type, ...}."
                ),
                "console": (
                    "list of {level, args, ts} captured during the "
                    "eval (log/info/warn/error/debug). args are "
                    "serialised the same way as `value`."
                ),
                "elapsed_ms": "int — wall time inside the browser",
                "timed_out": "bool",
                "error": (
                    "null if ok, else {name, message, stack}. Common: "
                    "ReferenceError when the page hasn't loaded the "
                    "thing you're referencing, or a timeout."
                ),
                "session_id": "echoed",
                "latency_ms": "int — bridge round-trip",
            },
            "examples": [
                {
                    "purpose": "Read the document title",
                    "js": "return document.title;",
                },
                {
                    "purpose": "Count <a> on the page",
                    "js": "return document.querySelectorAll('a').length;",
                },
                {
                    "purpose": "Wait for an element, then read it",
                    "js": (
                        "for (let i = 0; i < 30; i++) {"
                        "  const el = document.querySelector('#main');"
                        "  if (el) return el.innerText.slice(0, 500);"
                        "  await new Promise(r => setTimeout(r, 100));"
                        "}"
                        "throw new Error('#main never appeared');"
                    ),
                },
                {
                    "purpose": "Trigger a click",
                    "js": (
                        "document.querySelector('button.submit')?.click();"
                        "return 'clicked';"
                    ),
                },
                {
                    "purpose": "Dump localStorage",
                    "js": (
                        "const out = {};"
                        "for (let i = 0; i < localStorage.length; i++) {"
                        "  const k = localStorage.key(i);"
                        "  out[k] = localStorage.getItem(k);"
                        "}"
                        "return out;"
                    ),
                },
            ],
            "gotchas": [
                "The body is the function body, not an expression. "
                "`return value;` sends `value` back; a bare expression "
                "evaluates to undefined.",
                "Cross-origin iframes are not accessible from the host "
                "page's JS context.",
                "Errors thrown by your code come back as ok=false; the "
                "endpoint does not return HTTP 5xx for that.",
            ],
        },
    ],
    "workflow": [
        "1. GET /cli/sessions to find an active session_id.",
        "2. Optionally GET /cli/page?session_id=… to inspect HTML.",
        "3. POST /cli/eval to interact (read state, click, scrape).",
        "4. Repeat 2–3 as the page evolves.",
    ],
}
