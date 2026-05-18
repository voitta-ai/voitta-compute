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
from app.services import user_settings as _user_settings


router = APIRouter(prefix="/cli", tags=["cli"])


# ---- access guard ---------------------------------------------------------


_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def check_cli_access(request: Request) -> None:
    """Shared access check for /cli and /mcp.

    Three gates, in order: kill-switch (tray Settings), loopback peer,
    no browser Origin. Raises ``HTTPException`` with 403 on any miss.
    """
    if not _user_settings.mcp_cli_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "MCP/CLI debugging is disabled. Enable it from the "
                "Voitta tray icon → Settings."
            ),
        )
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


def _localhost_only(request: Request) -> None:
    check_cli_access(request)


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
            "Kill switch: disabled by default. Toggle from the Voitta "
            "tray icon → Settings (button: Enable/Disable MCP/CLI). "
            "When disabled, both /cli and /mcp return 403.",
        ],
    },
    "mcp": {
        "endpoint": "/mcp",
        "transport": "streamable-http",
        "tools": [
            "cli_sessions", "cli_page", "cli_eval", "cli_chat_state",
            "cli_screenshot", "cli_chat_inject", "cli_chat",
        ],
        "notes": [
            "Same kill switch and loopback guard as /cli.",
            "Tool semantics mirror the equivalent /cli/* REST routes.",
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
        {
            "method": "POST",
            "path": "/cli/chat",
            "description": (
                "Drive the LLM agent loop end-to-end and return the full "
                "transcript as JSON. Server-side; no chat-pane UI."
            ),
            "params": {
                "user": "string, required — the user message",
                "host": "string, optional — gates host-scoped plugins/tools",
                "session_id": "string, optional — needed for browser tools",
                "provider": "string, optional — defaults to settings.json",
                "model": "string, optional",
                "max_tokens": "int, optional",
                "max_tool_iterations": "int, optional",
            },
            "returns": {
                "ok": "bool",
                "transcript": "list of {role, ...}",
                "usage": "{input_tokens, output_tokens, …}",
                "iterations": "int",
                "stop_reason": "string",
            },
        },
        {
            "method": "POST",
            "path": "/cli/chat_inject",
            "description": (
                "Inject a message into a live chat pane as if the user "
                "typed it. The message + streamed response appear in the "
                "ChatPane UI; LLM runs through the regular /chat/stream "
                "path. Use this for watching CLI-driven interactions "
                "happen in real time (QA, learning, debugging)."
            ),
            "params": {
                "session_id": "string, required (from /cli/sessions)",
                "text": "string, required — the message text",
                "timeout_ms": "int, optional (default 10000)",
            },
            "returns": {"ok": "bool", "session_id": "string"},
            "notes": [
                "Fire-and-forget — does NOT wait for the LLM response.",
                "Watch the chat pane in the browser to see what happens.",
                "Use /cli/chat instead if you want the transcript as JSON.",
            ],
        },
    ],
    "workflow": [
        "1. GET /cli/sessions to find an active session_id.",
        "2. Optionally GET /cli/page?session_id=… to inspect HTML.",
        "3. POST /cli/eval to interact (read state, click, scrape).",
        "4. POST /cli/chat for server-side LLM runs returning JSON.",
        "5. POST /cli/chat_inject to mirror an LLM run into the chat UI.",
    ],
}


# ---- /cli/chat — drive the agent loop end-to-end --------------------------


class _CliChatRequest(BaseModel):
    """Same shape as the in-pane chat, but the response is a single JSON
    transcript rather than an SSE stream.

    For most CLI debugging you only need ``user`` (a single string).
    ``host`` controls which plugin's tools and prompt addenda are
    visible — without it, only host-agnostic tools are exposed.
    ``session_id`` is required when the LLM needs to call a
    browser-side tool (e.g. screenshot_report against a live tab).
    """
    user: str = Field(..., description="The user message — single turn.")
    host: str | None = Field(
        None, description="Hostname to scope plugins and host-gated tools."
    )
    session_id: str | None = Field(
        None, description="Bridge session for browser-side tools. Omit for server-only.",
    )
    system: str | None = Field(
        None, description="Override system prompt. Default: VOITTA_SYSTEM_PROMPT + plugin addenda.",
    )
    provider: str | None = Field(
        None, description="anthropic | openai | gemini. Default: settings.json provider.",
    )
    model: str | None = Field(
        None, description="Model id. Default: settings.json model for the chosen provider.",
    )
    max_tokens: int | None = None
    max_tool_iterations: int | None = Field(
        None, description="Cap on agent-loop iterations (tool-use rounds).",
    )


@router.get("/chat_state")
async def cli_chat_state(
    session_id: str = Query(..., description="From /cli/sessions."),
    timeout_ms: int = Query(10_000, ge=500, le=60_000),
    _: None = Depends(_localhost_only),
) -> dict:
    """Return a snapshot of the live chat pane state.

    Carries the full ``messages[]`` array (every user/assistant turn),
    the ``streaming`` flag (true while the LLM is still responding),
    any ``streaming_items`` (in-flight text deltas / tool calls), the
    current draft, and the last error. Useful for an external operator
    (Claude Code, video-recording script, etc.) to read what the LLM
    said in the chat pane without intercepting the SSE stream.

    Polling pattern: call repeatedly until ``streaming`` is ``false``
    and the most recent assistant message has settled.
    """
    try:
        result = await bridge.call(
            session_id, "read_chat_state", {}, timeout_ms=timeout_ms,
        )
    except ToolBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": exc.kind, "message": str(exc), "session_id": session_id},
        )
    if not result.ok:
        return {"ok": False, "session_id": session_id, "primitive_error": result.error}
    state = result.result if isinstance(result.result, dict) else {}
    return {"ok": True, "session_id": session_id, **state}


@router.post("/screenshot")
async def cli_screenshot(
    session_id: str = Query(..., description="From /cli/sessions."),
    timeout_ms: int = Query(60_000, ge=1_000, le=180_000),
    _: None = Depends(_localhost_only),
) -> dict:
    """Take a silent screenshot of the active report iframe.

    Calls the same ``screenshot_report`` primitive the chat LLM uses,
    but bypasses the chat — no message in the pane, no LLM turn. Returns
    base64-encoded webp + size metadata. Useful for video-recording
    workflows where the operator wants to peek at the report between
    LLM turns without triggering a chat turn.

    Errors with ``error: 'no_report'`` if no report is currently open;
    ``error: 'edit_mode'`` if the report is in edit mode (html2canvas
    can't rasterise the editable template's CSS).
    """
    try:
        result = await bridge.call(
            session_id, "screenshot_report", {}, timeout_ms=timeout_ms,
        )
    except ToolBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": exc.kind, "message": str(exc), "session_id": session_id},
        )
    if not result.ok:
        return {"ok": False, "session_id": session_id, "primitive_error": result.error}
    payload = result.result if isinstance(result.result, dict) else {}
    # The primitive returns ``data_url`` — strip the data: prefix so
    # callers can either decode directly or paste it back into <img>.
    data_url = payload.get("data_url") if isinstance(payload, dict) else None
    out: dict[str, Any] = {
        "ok": True, "session_id": session_id,
        "width": payload.get("width"), "height": payload.get("height"),
        "format": payload.get("format"),
        "nested_scenes_captured": payload.get("nested_scenes_captured"),
        "data_url": data_url,
    }
    return out


class _CliChatInjectRequest(BaseModel):
    session_id: str = Field(..., description="Bridge session id (see /cli/sessions).")
    text: str = Field(..., description="Message to inject as if the user typed it.")
    timeout_ms: int | None = Field(
        None, description="Bridge call timeout. Default 10s.",
    )


@router.post("/chat_inject")
async def cli_chat_inject(
    req: _CliChatInjectRequest, _: None = Depends(_localhost_only),
) -> dict:
    """Inject a message into the live chat pane of a connected session.

    Unlike ``/cli/chat`` (server-side, returns JSON), this endpoint
    pushes the message into the ChatPane in the browser via the
    bridge. The chat pane runs it through its regular
    ``/chat/stream`` path — message + streamed assistant response
    + tool calls + screenshots all appear in the UI exactly as if
    the user had typed it.

    Returns immediately after the message is queued; the LLM's
    response is delivered asynchronously into the chat pane, not
    awaited here. Use this when you want to watch a CLI-driven
    interaction happen in real time for QA / learning purposes.
    """
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    try:
        result = await bridge.call(
            req.session_id,
            "inject_chat_message",
            {"text": req.text},
            timeout_ms=req.timeout_ms or 10_000,
        )
    except ToolBridgeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": exc.kind, "message": str(exc), "session_id": req.session_id},
        )
    if not result.ok:
        return {
            "ok": False, "session_id": req.session_id,
            "primitive_error": result.error,
        }
    return {"ok": True, "session_id": req.session_id}


async def run_cli_chat(
    user: str,
    *,
    host: str | None = None,
    session_id: str | None = None,
    system: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    max_tool_iterations: int | None = None,
) -> dict:
    """Server-side LLM agent loop — shared between ``/cli/chat`` and
    the MCP ``cli_chat`` tool. Returns the same dict the REST route
    used to return inline. Raises ``HTTPException`` on config errors
    (no provider, no API key) so both surfaces map them to a 4xx.
    """
    import asyncio as _asyncio
    import json as _json

    from app.config import VOITTA_SYSTEM_PROMPT, settings as _settings
    from app.services import user_settings as _us
    from app.services.llm import (
        Message as _LlmMessage,
        NormalisedRequest as _NR,
        ProviderNotConfigured as _PNC,
        ToolSchema as _TS,
        default_model_for as _default_model_for,
        get_provider as _get_provider,
    )
    from app.services.llm.stream import (
        BlockDelta as _BD,
        BlockStart as _BS,
        BlockStop as _BSt,
        MessageStop as _MS,
        StreamError as _SE,
    )
    from app.services.render_log_drain import RenderDrain as _RD, format_reminder as _fmt
    from app.tools import registry as _registry
    from app.tools.providers import plugins_for_host as _plugins_for_host
    from app.tools.registry import ToolCtx as _ToolCtx

    blob = _us.read() if hasattr(_us, "read") else {}
    provider_id = provider or blob.get("provider")
    if not provider_id:
        raise HTTPException(
            status_code=400,
            detail="no provider — pass `provider` or save one in settings.json",
        )
    # Settings stores per-provider keys (anthropicApiKey, openaiApiKey, …).
    api_key = blob.get(f"{provider_id}ApiKey")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"no API key for provider {provider_id!r} in settings.json",
        )
    try:
        provider_impl = _get_provider(provider_id, api_key)  # type: ignore[arg-type]
    except _PNC as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    model_id = model or blob.get(f"{provider_id}Model") or _default_model_for(provider_id)  # type: ignore[arg-type]
    max_tokens_v = max_tokens or blob.get("maxTokens") or _settings.max_tokens
    max_iterations = min(
        max_tool_iterations or blob.get("maxToolIterations") or _settings.max_tool_iterations,
        _settings.max_tool_iterations_ceiling,
    )

    # Tool visibility + system prompt — mirror chat_stream.
    tools = [
        _TS(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in _registry.visible_for_host(host)
    ]
    system_v = system or VOITTA_SYSTEM_PROMPT
    for plugin in _plugins_for_host(host):
        addendum = plugin.get("system_prompt")
        if addendum:
            system_v = system_v.rstrip() + "\n\n" + addendum.rstrip()

    ctx = _ToolCtx(session_id=session_id)
    drain = _RD()
    messages: list[_LlmMessage] = [
        _LlmMessage(role="user", content=[{"type": "text", "text": user}])
    ]
    transcript: list[dict] = [{"role": "user", "text": user}]
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    iterations_run = 0
    final_stop = "max_iterations"

    for iteration in range(max_iterations):
        iterations_run = iteration + 1
        blocks_by_index: dict[int, dict] = {}
        text_buf: dict[int, list[str]] = {}
        args_buf: dict[int, list[str]] = {}
        iter_stop = "end_turn"

        async with provider_impl.stream(_NR(
            model=model_id, system=system_v, max_tokens=max_tokens_v,
            messages=messages, tools=tools,
        )) as events:
            async for ev in events:
                if isinstance(ev, _BS):
                    if ev.kind == "text":
                        blocks_by_index[ev.block_index] = {"type": "text", "text": ""}
                        text_buf[ev.block_index] = []
                    else:
                        blocks_by_index[ev.block_index] = {
                            "type": "tool_use",
                            "id": ev.tool_id or "",
                            "name": ev.tool_name or "",
                            "input": {},
                        }
                        args_buf[ev.block_index] = []
                elif isinstance(ev, _BD):
                    if ev.kind == "text":
                        text_buf.setdefault(ev.block_index, []).append(ev.text)
                    else:
                        args_buf.setdefault(ev.block_index, []).append(ev.text)
                elif isinstance(ev, _BSt):
                    b = blocks_by_index.get(ev.block_index)
                    if b is None:
                        continue
                    if b["type"] == "text":
                        b["text"] = "".join(text_buf.get(ev.block_index, []))
                    else:
                        joined = "".join(args_buf.get(ev.block_index, []))
                        if joined:
                            try:
                                b["input"] = _json.loads(joined)
                            except _json.JSONDecodeError:
                                b["input"] = {"_raw": joined}
                elif isinstance(ev, _MS):
                    iter_stop = ev.stop_reason
                    usage["input_tokens"] += ev.usage.input_tokens
                    usage["output_tokens"] += ev.usage.output_tokens
                    usage["cache_read_input_tokens"] += ev.usage.cache_read_input_tokens
                    usage["cache_creation_input_tokens"] += ev.usage.cache_creation_input_tokens
                elif isinstance(ev, _SE):
                    transcript.append({"role": "stream_error", "type": ev.type, "message": ev.message})
                    final_stop = f"stream_error:{ev.type}"
                    return {
                        "ok": False, "transcript": transcript,
                        "usage": usage, "iterations": iterations_run,
                        "stop_reason": final_stop,
                    }

        assistant_content: list[dict] = []
        for idx in sorted(blocks_by_index.keys()):
            b = blocks_by_index[idx]
            if b["type"] == "text" and not b.get("text"):
                continue
            assistant_content.append(b)
            if b["type"] == "text":
                transcript.append({"role": "assistant", "text": b["text"]})

        if iter_stop != "tool_use":
            messages.append(_LlmMessage(role="assistant", content=assistant_content))
            final_stop = iter_stop
            break

        tool_uses = [(i, b) for i, b in blocks_by_index.items() if b["type"] == "tool_use"]
        if not tool_uses:
            final_stop = "protocol_error"
            break

        results = await _asyncio.gather(*[
            _registry.dispatch(tu["name"], dict(tu.get("input") or {}), ctx)
            for _, tu in tool_uses
        ])

        tool_result_blocks: list[dict] = []
        for (_, tu), res in zip(tool_uses, results):
            payload = res.result if res.ok else {"error": res.error}
            drain.note_tool_result(tu["name"] or "", payload)
            transcript.append({
                "role": "tool_use", "name": tu["name"], "id": tu["id"],
                "input": tu.get("input") or {},
            })
            transcript.append({
                "role": "tool_result", "name": tu["name"], "id": tu["id"],
                "ok": res.ok, "latency_ms": res.latency_ms,
                "error": res.error, "result": payload,
            })
            text = payload if isinstance(payload, str) else _json.dumps(
                payload, ensure_ascii=False, default=str
            )
            tool_result_blocks.append({
                "type": "tool_result", "tool_use_id": tu["id"],
                "content": text, "is_error": not res.ok, "_name": tu["name"],
            })

        messages.append(_LlmMessage(role="assistant", content=assistant_content))
        drained = drain.drain()
        reminder = _fmt(drained)
        if reminder:
            tool_result_blocks.insert(0, {"type": "text", "text": reminder})
            transcript.append({"role": "system_reminder", "text": reminder, "events": drained})
        messages.append(_LlmMessage(role="user", content=tool_result_blocks))

    return {
        "ok": True, "transcript": transcript, "usage": usage,
        "iterations": iterations_run, "stop_reason": final_stop,
    }


@router.post("/chat")
async def cli_chat(req: _CliChatRequest, _: None = Depends(_localhost_only)) -> dict:
    """Drive the LLM agent loop end-to-end without an SSE stream.

    Thin wrapper around :func:`run_cli_chat` — the same body powers the
    MCP ``cli_chat`` tool. See ``run_cli_chat`` for behaviour notes.
    """
    return await run_cli_chat(
        user=req.user,
        host=req.host,
        session_id=req.session_id,
        system=req.system,
        provider=req.provider,
        model=req.model,
        max_tokens=req.max_tokens,
        max_tool_iterations=req.max_tool_iterations,
    )
