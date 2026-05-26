"""Mount the embedded FastMCP debugging server at ``/mcp``.

Streamable-HTTP transport on the existing FastAPI listener — no new
port. Three layers of gate, in order:

  1. ``mcpDebugEnabled`` user setting (tray-bar Settings toggle).
  2. Loopback-only peer (``127.0.0.1`` or ``::1``).
  3. No browser ``Origin`` header — use a CLI / desktop MCP client,
     not a tab. Defends against drive-by JS calls from a malicious
     page that already got past our loopback check via DNS rebinding.

Same contract as the legacy bookmarklet's ``/mcp`` route; the CLI
half of that infra (``/cli/*``) is intentionally not ported.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services import user_settings as _user_settings


_log = logging.getLogger(__name__)


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _refuse(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "forbidden", "message": message},
        status_code=status,
    )


class _MCPGate:
    """ASGI middleware: enforce localhost + Origin-absent + kill switch."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not _user_settings.mcp_debug_enabled():
            await _refuse(
                403,
                "MCP debugging is disabled. Enable it from the Voitta tray "
                "icon → Settings → 'Enable MCP debugging'.",
            )(scope, receive, send)
            return
        request = Request(scope, receive)
        peer = (request.client.host if request.client else "") or ""
        if peer not in _LOOPBACK_HOSTS:
            await _refuse(
                403, f"/mcp accepts loopback only (peer={peer!r})"
            )(scope, receive, send)
            return
        if request.headers.get("origin"):
            await _refuse(
                403,
                "/mcp rejects browser Origin (use a CLI / desktop MCP "
                "client, not a tab)",
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_mcp_asgi():
    """Return the gated FastMCP Starlette app, ready to mount at ``/mcp``.

    FastMCP exposes its streamable-HTTP transport at the root of its
    own Starlette app. We wrap that in our access-gate middleware and
    return the result.
    """
    from app.services.mcp_server import get_server

    mcp = get_server()
    inner = mcp.http_app(path="/", stateless_http=True, transport="http")
    inner.add_middleware(_MCPGate)
    return inner
