"""HTTP endpoints for the server↔browser tool bridge.

Endpoints:

* ``GET  /tools/inbox?session_id=<sid>`` — long-lived SSE; server → browser tool requests.
* ``POST /tools/register`` — capability + identification handshake.
* ``POST /tools/result`` — browser → server result delivery.
* ``POST /tools/test/echo`` — debug: invoke a primitive and return its result.
* ``GET  /tools/sessions`` — debug: list connected sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app.bridge import ToolBridgeError, bridge
from app.bridge.models import (
    RegisterRequest,
    RegisterResponse,
    TestEchoRequest,
    ToolResultEnvelope,
)


router = APIRouter(prefix="/tools", tags=["tools"])
logger = logging.getLogger(__name__)


PING_INTERVAL_SECONDS = 20.0


@router.get("/inbox")
async def inbox(session_id: str = Query(..., min_length=8, max_length=128)) -> EventSourceResponse:
    """Open the long-lived SSE inbox for a session.

    The first connect creates the session bucket. Subsequent connects with
    the same session id share the bucket (the browser's EventSource will
    auto-reconnect on transient drops). Events are pulled from the
    session's asyncio.Queue; idle connections receive a ``ping`` every 20s
    to keep proxies from collapsing the stream.
    """

    session = await bridge.get_or_create(session_id)
    session.connected = True
    session.inbox_streams += 1

    async def event_stream() -> AsyncIterator[dict]:
        # First event tells the browser the bucket is live so it can post register.
        yield {"event": "ready", "data": json.dumps({"session_id": session_id})}
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        session.inbox.get(), timeout=PING_INTERVAL_SECONDS
                    )
                    yield {"event": msg["event"], "data": json.dumps(msg["data"])}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        except asyncio.CancelledError:
            raise
        finally:
            await bridge.disconnect(session_id)

    return EventSourceResponse(event_stream())


@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest) -> RegisterResponse:
    """Capability + identification handshake.

    The browser POSTs this after the inbox SSE opens. We accept it
    even if the bucket doesn't exist yet (or has been wiped by a
    backend restart) — ``bridge.register`` recreates the bucket. The
    next /tools/inbox attach flips ``connected`` to True and the
    session is fully restored without forcing the browser to mint a
    new session_id.
    """

    identification = {
        "page": req.page.model_dump() if req.page else None,
        "user_agent": req.user_agent,
        "viewport": req.viewport.model_dump() if req.viewport else None,
        "release_tag": req.release_tag,
    }
    bridge.register(req.session_id, req.capabilities, identification)
    logger.info(
        "session %s registered: caps=%s page=%s",
        req.session_id,
        req.capabilities,
        identification["page"]["pathname"] if identification["page"] else None,
    )
    # Tell the browser which tools we'll expose. Pulled from the registry so
    # the diagnostic stays accurate as new tools are added.
    from app.tools import registry

    return RegisterResponse(session_id=req.session_id, tools=registry.names())


@router.post("/result")
async def result(env: ToolResultEnvelope) -> dict:
    """Browser → server: deliver the result of a tool call."""

    delivered = bridge.deliver_result(
        env.session_id, env.call_id, env.ok, env.result, env.error
    )
    if not delivered:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_call_id", "call_id": env.call_id}
        )
    return {"ok": True}


# ---- debug endpoints -------------------------------------------------------


@router.get("/sessions")
async def list_sessions() -> dict:
    return {"sessions": bridge.list_sessions()}


@router.post("/test/echo")
async def test_echo(req: TestEchoRequest) -> dict:
    """Debug: invoke a browser primitive on the given session.

    Used by ``scripts/fake_browser.py`` and from curl during smoke tests:

        curl -k -X POST https://127.0.0.1:12358/tools/test/echo \\
             -H 'content-type: application/json' \\
             -d '{"session_id":"...","primitive":"get_url","args":{}}'
    """

    try:
        res = await bridge.call(req.session_id, req.primitive, req.args, req.timeout_ms)
    except ToolBridgeError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.kind, "message": str(exc)})
    return {
        "ok": res.ok,
        "latency_ms": res.latency_ms,
        "result": res.result,
        "error": res.error,
    }
