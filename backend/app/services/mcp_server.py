"""Embedded MCP server — exposes the ``/cli`` back-channel as MCP tools.

Lives on the same port as the FastAPI backend, mounted at ``/mcp``.
Same loopback-only + Origin-blocked + ``mcp_cli_enabled()`` gate as
the REST ``/cli`` routes (see :func:`app.routes.cli.check_cli_access`).

Each tool here is a thin wrapper around the same primitives the REST
handlers call (``bridge.call`` and :func:`run_cli_chat`). No business
logic is duplicated — if the REST contract changes, the MCP tool
changes with it.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.bridge import ToolBridgeError, bridge


_SERVER: FastMCP | None = None


def _err(kind: str, message: str, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "error": kind, "message": message}
    out.update(extra)
    return out


async def _call_bridge(
    session_id: str, primitive: str, args: dict, timeout_ms: int
) -> dict:
    try:
        result = await bridge.call(
            session_id, primitive, args, timeout_ms=timeout_ms
        )
    except ToolBridgeError as exc:
        return _err(exc.kind, str(exc), session_id=session_id)
    if not result.ok:
        return _err(
            "primitive_failed",
            "browser primitive failed",
            session_id=session_id,
            primitive_error=result.error,
        )
    payload = result.result if isinstance(result.result, dict) else {}
    payload.setdefault("ok", True)
    payload["session_id"] = session_id
    payload["latency_ms"] = result.latency_ms
    return payload


def get_server() -> FastMCP:
    """Lazy singleton — FastMCP instance with all CLI tools registered."""
    global _SERVER
    if _SERVER is not None:
        return _SERVER

    mcp = FastMCP("voitta-bookmarklet-cli")

    @mcp.tool()
    async def cli_sessions() -> dict:
        """List every bookmarklet session the backend knows about.

        Returns ``{count, sessions: [{session_id, connected, url, host,
        pathname, title, user_agent, capabilities, pending_calls,
        created_at, last_seen, raw}]}``. Use the ``session_id`` from
        each entry as input to the other tools.
        """
        sessions = bridge.list_sessions()
        flat: list[dict[str, Any]] = []
        for s in sessions:
            ident = s.get("identification") or {}
            page = ident.get("page") or {}
            flat.append({
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
            })
        return {"count": len(flat), "sessions": flat}

    @mcp.tool()
    async def cli_page(session_id: str, timeout_ms: int = 15_000) -> dict:
        """Return URL + title + full ``<html>`` outerHTML for one session.

        Single round-trip via the ``get_page_dump`` browser primitive.
        No size cap on returned HTML. If the bookmarklet tab is
        suspended, returns ``{ok: false, error: 'no_session'}``.
        """
        payload = await _call_bridge(
            session_id, "get_page_dump", {}, timeout_ms
        )
        if not payload.get("ok"):
            return payload
        return {
            "ok": True,
            "session_id": session_id,
            "url": payload.get("url"),
            "title": payload.get("title"),
            "pathname": payload.get("pathname"),
            "search": payload.get("search"),
            "hash": payload.get("hash"),
            "user_agent": payload.get("user_agent"),
            "html": payload.get("html"),
            "html_bytes": len(payload.get("html") or ""),
            "fetched_at_ms": payload.get("ts"),
            "latency_ms": payload.get("latency_ms"),
        }

    @mcp.tool()
    async def cli_eval(
        session_id: str,
        js: str,
        await_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run arbitrary JavaScript in a bookmarklet tab.

        The code body is wrapped in an ``AsyncFunction`` — ``await`` at
        the top level works, and ``return X`` sends ``X`` back. Console
        output is captured; throws come back as ``ok=false`` rather
        than 5xx. See ``/cli/eval`` docs for the serialisation rules
        (DOM nodes, Map/Set, BigInt, … all wrapped as ``{__type, ...}``).
        """
        eff_await = await_ms if await_ms and await_ms > 0 else 30_000
        eff_timeout = (
            timeout_ms if timeout_ms and timeout_ms > 0 else eff_await + 5_000
        )
        return await _call_bridge(
            session_id,
            "eval_js",
            {"js": js, "await_ms": eff_await},
            eff_timeout,
        )

    @mcp.tool()
    async def cli_chat_state(
        session_id: str, timeout_ms: int = 10_000
    ) -> dict:
        """Snapshot of the live chat pane state.

        Returns ``messages[]`` (every turn), ``streaming``,
        ``streaming_items``, the draft, and the last error. Poll until
        ``streaming`` is false to observe an injected message running
        to completion.
        """
        return await _call_bridge(
            session_id, "read_chat_state", {}, timeout_ms
        )

    @mcp.tool()
    async def cli_screenshot(
        session_id: str, timeout_ms: int = 60_000
    ) -> dict:
        """Silent screenshot of the active report iframe.

        Calls the same ``screenshot_report`` primitive the chat LLM
        uses, but bypasses chat — no message in the pane, no LLM turn.
        Returns base64 webp + size metadata.
        """
        payload = await _call_bridge(
            session_id, "screenshot_report", {}, timeout_ms
        )
        if not payload.get("ok"):
            return payload
        return {
            "ok": True,
            "session_id": session_id,
            "width": payload.get("width"),
            "height": payload.get("height"),
            "format": payload.get("format"),
            "nested_scenes_captured": payload.get("nested_scenes_captured"),
            "data_url": payload.get("data_url"),
        }

    @mcp.tool()
    async def cli_chat_inject(
        session_id: str, text: str, timeout_ms: int = 10_000
    ) -> dict:
        """**PREFERRED** way to drive the agent — inject a message into
        the live bookmarklet chat pane as if the user typed it.

        The message + streamed assistant response + tool calls all
        appear in the ChatPane UI, exactly as if a human typed them.
        Fire-and-forget: returns as soon as the message is queued; the
        LLM response streams asynchronously into the pane. Poll
        ``cli_chat_state`` if you need to know when the turn settled.

        Use this for normal driving / debugging / demoing. Reach for
        ``cli_chat`` ONLY when you specifically need a headless,
        non-UI run with the full transcript returned as JSON (e.g.
        an unattended script integration test).
        """
        if not text or not text.strip():
            return _err("bad_request", "text is required")
        return await _call_bridge(
            session_id, "inject_chat_message", {"text": text}, timeout_ms
        )

    @mcp.tool()
    async def cli_chat(
        user: str,
        host: str | None = None,
        session_id: str | None = None,
        system: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_tool_iterations: int | None = None,
    ) -> dict:
        """**HEADLESS LAST RESORT** — drive the LLM agent loop server-side
        and return the full transcript as a single JSON blob.

        Prefer ``cli_chat_inject`` for almost everything: it puts the
        message into the live chat pane so you can WATCH the streaming
        response, tool calls, screenshots, and errors as they happen.
        ``cli_chat`` is silent until it finishes — a multi-iteration
        agent run can take 30–120s with zero feedback, which is
        frustrating to debug.

        Use ``cli_chat`` ONLY when you genuinely need:
          • a non-UI run (no browser tab attached to the session), or
          • the structured transcript as a return value for an
            unattended integration test / CI script, or
          • a different provider/model than the user's chat-pane
            settings.

        Browser-side tools require ``session_id`` + a connected
        bookmarklet session. Returns
        ``{ok, transcript, usage, iterations, stop_reason}``.
        """
        # Imported here, not at module top, to avoid pulling the agent
        # loop's transitive deps into the import graph of anything that
        # only wants to introspect the MCP tool list.
        from fastapi import HTTPException

        from app.routes.cli import run_cli_chat

        try:
            return await run_cli_chat(
                user=user,
                host=host,
                session_id=session_id,
                system=system,
                provider=provider,
                model=model,
                max_tokens=max_tokens,
                max_tool_iterations=max_tool_iterations,
            )
        except HTTPException as exc:
            return _err("config_error", str(exc.detail))

    _SERVER = mcp
    return mcp
