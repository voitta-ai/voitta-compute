"""Routes that surface loaded plugins + MCP connector status to the UI.

These power the tabbed Settings panel:

* ``GET /api/plugins``                    — list every plugin loaded at
  startup, with its manifest, settings_schema, and the live MCP
  connector status for each declared server.
* ``POST /api/plugins/{name}/refresh``    — re-probe every MCP connector
  belonging to the named plugin (the "Refresh tool list" button).

Refresh is the *only* time we re-list remote tools, per design contract:
startup + explicit refresh, never per chat turn.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.mcp import (
    MCPConnector,
    list_connectors,
    refresh_all,
    refresh_one,
)
from app.tools.providers import LOADED_PLUGINS

_logger = logging.getLogger(__name__)

router = APIRouter()


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


def _plugin_to_dict(plugin: dict[str, Any]) -> dict[str, Any]:
    """Project a loaded-plugin record into the wire shape.

    Manifest field passthrough is selective: we expose what the UI
    needs (name, version, description, agent_name, host_patterns,
    settings_schema, hide_brand, default_layout). The full manifest
    isn't useful to the client and may grow private fields over time.
    """
    name = plugin["name"]
    m = plugin["manifest"]
    return {
        "name": name,
        "version": m.get("version"),
        "description": m.get("description"),
        "agent_name": m.get("agent_name"),
        "host_patterns": m.get("host_patterns") or [],
        "settings_schema": m.get("settings_schema"),
        "hide_brand": bool(m.get("hide_brand")),
        "default_layout": m.get("default_layout"),
        # Custom-panel opt-out for plugins that need a hand-rolled
        # settings UI (Google's OAuth dance). The frontend uses this
        # to choose between the schema renderer and the plugin-shipped
        # Preact component. Today we declare it via a manifest hint
        # ``settings_panel: "custom"``; absent → schema-rendered.
        "settings_panel": m.get("settings_panel") or "schema",
        "mcp_connectors": [
            _connector_to_dict(c) for c in list_connectors(plugin_name=name)
        ],
    }


@router.get("/api/plugins")
async def list_plugins() -> dict[str, Any]:
    """List every loaded plugin + MCP connector status.

    Cached only as much as the underlying state allows: connector
    status changes only on explicit refresh, so the response is stable
    until then. Plugin list is fixed at startup. Cheap enough to call
    on every Settings panel open without a cache layer.
    """
    return {
        "plugins": [_plugin_to_dict(p) for p in LOADED_PLUGINS],
    }


@router.post("/api/plugins/{name}/refresh")
async def refresh_plugin(name: str) -> dict[str, Any]:
    """Re-probe every MCP connector belonging to one plugin.

    Returns the post-refresh connector summary so the UI can update
    status badges without a follow-up GET. Returns 404 if no plugin
    of that name was loaded; returns 200 with an empty connectors
    list if the plugin is loaded but has zero MCP servers (the UI
    can interpret this as a no-op).
    """
    if not any(p["name"] == name for p in LOADED_PLUGINS):
        raise HTTPException(status_code=404, detail=f"No plugin named {name!r}")
    conns = list_connectors(plugin_name=name)
    refreshed: list[MCPConnector] = []
    for conn in conns:
        refreshed.append(await refresh_one(conn))
    return {
        "plugin": name,
        "connectors": [_connector_to_dict(c) for c in refreshed],
    }


@router.post("/api/plugins/refresh-all")
async def refresh_all_plugins() -> dict[str, Any]:
    """Re-probe every MCP connector across every plugin.

    Mostly useful for the developer reload path / desktop ``Refresh
    everything`` menu item; the per-plugin route is what the Settings
    panel hits.
    """
    refreshed = await refresh_all()
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    for c in refreshed:
        by_plugin.setdefault(c.decl.plugin_name, []).append(_connector_to_dict(c))
    return {"plugins": by_plugin}
