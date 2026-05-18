"""Mount the embedded FastMCP server at ``/mcp``.

Streamable-HTTP transport on the existing FastAPI listener (port 12358
in production) — no new port. The same kill switch and loopback guard
as :mod:`app.routes.cli` apply; see
:func:`app.routes.cli.check_cli_access`.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.routes.cli import _LOOPBACK_HOSTS
from app.services import user_settings as _user_settings


_log = logging.getLogger(__name__)


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
        if not _user_settings.mcp_cli_enabled():
            await _refuse(
                403,
                "MCP/CLI debugging is disabled. Enable it from the "
                "Voitta tray icon → Settings.",
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
                403, "/mcp rejects browser Origin (use a CLI tool, not a tab)"
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_mcp_asgi() -> Starlette:
    """Return the gated FastMCP Starlette app, ready to mount at ``/mcp``.

    FastMCP exposes its streamable-HTTP transport at the root of its
    own Starlette app. We wrap that in our access-gate middleware and
    return the result to ``main.py`` for mounting.
    """
    from app.services.mcp_server import get_server

    mcp = get_server()
    # ``path=""`` puts the streamable-HTTP endpoint at the root of the
    # sub-app, so the mount point ``/mcp`` becomes the full URL.
    inner = mcp.http_app(path="/", stateless_http=True, transport="http")
    inner.add_middleware(_MCPGate)
    return inner
