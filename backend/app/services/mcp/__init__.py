"""MCP client infrastructure.

Plugins declare ``mcp_servers`` entries in ``manifest.json``. At startup
the plugin loader (``app.tools.providers``) parses those entries and
calls :func:`register_connector` for each one. The first call to
:func:`refresh_all` (typically issued from the FastAPI startup event
or from ``POST /api/plugins/<name>/refresh``) opens a streamable-http
connection to every server, lists its tools, and synthesises a
``ToolSpec`` for each remote tool into the shared registry.

The remote tool list is refreshed lazily — never per chat turn. Per
the design contract: startup + explicit refresh button only. Tool
*calls* however always pull the live token from the settings file, so
the user can update credentials without restarting the backend.
"""

from __future__ import annotations

from .registry import (  # noqa: F401  re-exports for callers
    MCP_CONNECTORS,
    MCPConnector,
    MCPServerDecl,
    get_connector,
    list_connectors,
    refresh_all,
    refresh_one,
    register_connector,
    unregister_plugin,
)
