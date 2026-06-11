"""Routes surfacing loaded plugins + MCP connector status to the
Settings UI.

* ``GET  /api/plugins``                    — list of plugins loaded at
  startup, each with manifest metadata, settings_schema, and the live
  MCP connector status for any servers it declared.
* ``POST /api/plugins/{name}/refresh``     — re-probe every MCP
  connector belonging to one plugin (the "Refresh tool list" button).
* ``POST /api/plugins/refresh-all``        — re-probe everything (used
  at startup and for the developer-reload escape hatch).

Refresh is the only time remote tools are re-listed, per design
contract: startup + explicit refresh, never per chat turn.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.plugins import Plugin, all_plugins
from app.services import host_activation
from app.services.mcp.registry import (
    MCPConnector,
    list_connectors,
    refresh_all,
    refresh_one,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins")


def _connector_to_dict(conn: MCPConnector) -> dict[str, Any]:
    d = conn.decl
    return {
        "id": d.id,
        "url_setting": d.url_setting,
        "token_setting": d.token_setting,
        "tool_prefix": d.tool_prefix,
        "status": conn.status,
        "last_error": conn.last_error,
        "tool_names": conn.tool_names,
        "tool_count": len(conn.tool_names),
    }


def _plugin_to_dict(p: Plugin, extra_hosts: list[str]) -> dict[str, Any]:
    """Project a loaded-plugin record into the wire shape."""
    return {
        "name": p.name,
        "rel_dir": p.rel_dir,
        "version": p.version,
        "description": p.description,
        "agent_name": p.agent_name,
        "host_patterns": list(p.host_patterns),
        # User-added activation hosts (Settings → Plugins tab).
        "extra_hosts": extra_hosts,
        "settings_schema": p.settings_schema,
        # Custom-panel opt-out for plugins that need a hand-rolled
        # settings UI (Google's OAuth dance). The FE uses this to choose
        # between the schema renderer and the plugin-shipped React
        # component.
        "settings_panel": p.settings_panel,
        "mcp_connectors": [
            _connector_to_dict(c) for c in list_connectors(plugin_name=p.name)
        ],
    }


@router.get("")
async def list_plugins() -> dict[str, Any]:
    """List every loaded plugin + MCP connector status.

    Connector status changes only on explicit refresh, so the response
    is stable until the user hits the Refresh button. Plugin list is
    fixed at startup; ``extra_hosts`` tracks the live settings blob.
    """
    extra_map = host_activation.extra_hosts_map()
    return {
        "plugins": [
            _plugin_to_dict(p, extra_map.get(p.name, [])) for p in all_plugins()
        ]
    }


@router.post("/{name}/refresh")
async def refresh_plugin(name: str) -> dict[str, Any]:
    """Re-probe every MCP connector belonging to one plugin.

    Returns the post-refresh connector summary so the UI can update
    status badges without a follow-up GET. 404 if no plugin of that
    name was loaded; 200 with an empty connectors list if the plugin
    is loaded but declares zero MCP servers.
    """
    if not any(p.name == name for p in all_plugins()):
        raise HTTPException(status_code=404, detail=f"No plugin named {name!r}")
    conns = list_connectors(plugin_name=name)
    refreshed: list[MCPConnector] = []
    for conn in conns:
        refreshed.append(await refresh_one(conn))
    return {
        "plugin": name,
        "connectors": [_connector_to_dict(c) for c in refreshed],
    }


@router.post("/refresh-all")
async def refresh_all_plugins() -> dict[str, Any]:
    """Re-probe every MCP connector across every plugin.

    Called once at FastAPI startup so the catalogue lands warm.
    """
    refreshed = await refresh_all()
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    for c in refreshed:
        by_plugin.setdefault(c.decl.plugin_name, []).append(_connector_to_dict(c))
    return {"plugins": by_plugin}
