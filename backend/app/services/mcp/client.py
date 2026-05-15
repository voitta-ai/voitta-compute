"""Thin wrapper around ``fastmcp.Client`` for streamable-http MCP servers.

Each call opens a short-lived client (the streamable-http transport is
cheap to set up — one POST per request internally), avoids carrying
long-lived sessions through the chat dispatch path, and dies cleanly on
timeout. Long-lived sessions would also force us to think about
reconnection on token rotation; we keep the lifecycle simple instead.

The wrapper exists separately from the connector registry so the
connector layer stays test-friendly: tests can stub :func:`list_tools`
and :func:`call_tool` without touching network code.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import CallToolResult, Tool

_logger = logging.getLogger(__name__)

# Wire-level timeouts. The MCP server is local-network at worst (rag-
# enterprise typically lives on the same host or a same-VPC box) so
# tight defaults are reasonable. The chat loop already protects the
# overall turn with its own iteration cap; we trust the user to override
# at the connector level if a particular server needs more.
DEFAULT_TIMEOUT_SECONDS = 30.0
LIST_TOOLS_TIMEOUT_SECONDS = 10.0


def _make_client(url: str, bearer: str | None, timeout: float) -> Client:
    """Construct a fastmcp Client targeting ``url`` with optional bearer auth.

    ``bearer`` is the token string itself (no ``Bearer `` prefix);
    fastmcp's StreamableHttpTransport prefixes it. ``None`` skips auth
    entirely — useful for local dev (rag-enterprise with
    ``VOITTA_SINGLE_USER`` / ``VOITTA_DEV_USER``).
    """
    transport = StreamableHttpTransport(url=url, auth=bearer or None)
    return Client(transport, timeout=timeout)


async def list_tools(url: str, bearer: str | None) -> list[Tool]:
    """List the remote MCP server's tools. Raises on connection failure.

    Caller (the connector registry) is responsible for catching, classifying,
    and surfacing the failure to the UI as a status badge.
    """
    async with _make_client(url, bearer, LIST_TOOLS_TIMEOUT_SECONDS) as c:
        return await c.list_tools()


async def call_tool(
    url: str,
    bearer: str | None,
    name: str,
    arguments: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CallToolResult:
    """Invoke a remote MCP tool. Returns the raw CallToolResult.

    Result normalisation (extracting text/image blocks into the
    bookmarklet's tool-result envelope) lives in
    :mod:`app.services.mcp.tool_adapter` — keep the transport and the
    shape-mapping concerns separate so the latter is unit-testable
    without a server.
    """
    async with _make_client(url, bearer, timeout) as c:
        # raise_on_error=False: we surface errors as a wrapped result so
        # the chat loop sees a tool failure (with the server's text), not
        # a Python exception. The bookmarklet's per-tool error envelope
        # is more useful to the LLM than a stack trace.
        return await c.call_tool(name, arguments, raise_on_error=False)
