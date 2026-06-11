"""Connector registry — one entry per ``mcp_servers[]`` declaration.

A connector is the materialised handle for a manifest entry: it knows
where the server lives (via a settings key), how to read the bearer
token (also via a settings key), what host_pattern gates the tools,
and the synthetic ToolSpec names it owns.

Lifecycle:

* Plugin loader calls :func:`register_connector` once per declaration
  it finds in ``manifest.json``. No network at this point.
* The first call to :func:`refresh_all` (FastAPI startup, or explicit
  ``POST /api/plugins/<name>/refresh``) opens each connector, lists
  tools, and synthesises ToolSpecs into the shared registry.
* Subsequent refreshes re-list and replace; we remove only the
  connector's own synthesised tools so we don't disturb local Python
  tools the plugin also registered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.services import user_settings
from app.tools.registry import ToolSpec, registry as _tool_registry

from . import client as mcp_client
from .tool_adapter import synth_tool_spec

_logger = logging.getLogger(__name__)


# -- manifest decl -----------------------------------------------------------


@dataclass
class MCPServerDecl:
    """Frozen interpretation of one ``manifest.mcp_servers[]`` entry.

    The plugin loader builds these from raw manifest JSON; the registry
    materialises a runtime :class:`MCPConnector` from each.
    """

    plugin_name: str
    id: str                    # connector id within the plugin
    url_setting: str           # dot-path into the settings blob
    token_setting: str | None  # dot-path; None means no auth needed
    tool_prefix: str
    expose_tools: list[str] | None  # None / "*" → all; else allowlist
    host_patterns: list[str]
    # Future: transport variants. Today only "streamable-http".
    transport: str = "streamable-http"

    @classmethod
    def from_manifest_entry(
        cls,
        *,
        plugin_name: str,
        host_patterns: list[str],
        raw: dict[str, Any],
    ) -> "MCPServerDecl":
        """Parse one ``mcp_servers[]`` entry. Raises on malformed input.

        The plugin loader catches the exception and logs it; the plugin's
        other tools still register, only this connector is skipped. We
        prefer hard validation here over silent fall-throughs so a typo'd
        manifest is visible at startup, not three chat turns later.
        """
        if not isinstance(raw, dict):
            raise ValueError("mcp_servers entry must be an object")
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("mcp_servers entry missing required field 'id'")
        url_setting = str(raw.get("url_setting") or "").strip()
        if not url_setting:
            raise ValueError(
                f"mcp_servers[{cid}] missing required field 'url_setting'"
            )
        auth = raw.get("auth") or {}
        token_setting: str | None = None
        if isinstance(auth, dict) and auth.get("type") == "bearer":
            token_setting = str(auth.get("token_setting") or "").strip() or None

        tool_prefix = str(raw.get("tool_prefix") or f"{plugin_name}_")
        expose = raw.get("expose_tools")
        expose_tools: list[str] | None
        if expose is None or expose == "*":
            expose_tools = None
        elif isinstance(expose, list):
            expose_tools = [str(t) for t in expose if isinstance(t, str)]
        else:
            raise ValueError(
                f"mcp_servers[{cid}].expose_tools must be '*', omitted, or a list"
            )

        transport = str(raw.get("transport") or "streamable-http")
        if transport != "streamable-http":
            raise ValueError(
                f"mcp_servers[{cid}].transport={transport!r} not supported "
                "(only 'streamable-http' today)"
            )

        return cls(
            plugin_name=plugin_name,
            id=cid,
            url_setting=url_setting,
            token_setting=token_setting,
            tool_prefix=tool_prefix,
            expose_tools=expose_tools,
            host_patterns=host_patterns,
            transport=transport,
        )


# -- runtime connector -------------------------------------------------------


@dataclass
class MCPConnector:
    """Runtime state for one MCP server connection.

    Holds the manifest declaration plus the last probe result so the
    settings UI can render an at-a-glance status badge (connected /
    unauthorized / unreachable / not-configured) without re-probing on
    every page render.
    """

    decl: MCPServerDecl
    # Status: "unknown" | "ok" | "unauth" | "unreachable" | "not_configured"
    status: str = "unknown"
    last_error: str | None = None
    tool_names: list[str] = field(default_factory=list)
    # Names of synthetic ToolSpecs we own in the global registry.
    # Tracked separately so a refresh can remove only ours.
    owned_tool_names: set[str] = field(default_factory=set)


# -- module-global registry --------------------------------------------------

# Populated by the plugin loader at startup. Keyed by
# ``"<plugin_name>:<connector_id>"`` so a plugin can declare multiple
# servers without collision.
MCP_CONNECTORS: dict[str, MCPConnector] = {}


def _key(plugin_name: str, connector_id: str) -> str:
    return f"{plugin_name}:{connector_id}"


def register_connector(decl: MCPServerDecl) -> MCPConnector:
    """Register a connector from a manifest declaration. Idempotent.

    Called by the plugin loader during ``_import_plugin``. Doesn't
    open any connections — just records the declaration so a later
    :func:`refresh_all` can act on it.
    """
    k = _key(decl.plugin_name, decl.id)
    existing = MCP_CONNECTORS.get(k)
    if existing is not None:
        # Re-registration replaces the declaration but preserves the
        # owned-tools set so a plugin reload (future feature) can swap
        # them atomically. Today plugins only register once at startup.
        existing.decl = decl
        return existing
    conn = MCPConnector(decl=decl)
    MCP_CONNECTORS[k] = conn
    return conn


def get_connector(plugin_name: str, connector_id: str) -> MCPConnector | None:
    return MCP_CONNECTORS.get(_key(plugin_name, connector_id))


def list_connectors(plugin_name: str | None = None) -> list[MCPConnector]:
    """List connectors, optionally filtered to one plugin."""
    if plugin_name is None:
        return list(MCP_CONNECTORS.values())
    return [c for c in MCP_CONNECTORS.values() if c.decl.plugin_name == plugin_name]


def unregister_plugin(plugin_name: str) -> None:
    """Drop every connector belonging to a plugin (used by hot-reload paths).

    Not called at startup; kept for forward compatibility when we add a
    "reload plugin" admin button. Removes the synthesised ToolSpecs from
    the global registry too.
    """
    victims = [k for k, c in MCP_CONNECTORS.items() if c.decl.plugin_name == plugin_name]
    for k in victims:
        conn = MCP_CONNECTORS.pop(k)
        _drop_owned_tools(conn)


# -- settings access ---------------------------------------------------------


def _dotted_get(blob: dict[str, Any], path: str) -> Any:
    """Walk ``a.b.c`` through nested dicts; return None for misses.

    Manifest entries reference settings via dot-paths like
    ``plugins.voitta-enterprise.mcp.url``. The settings file itself
    stores them as the natural nested JSON shape — no flattening at
    the storage layer. This helper is the runtime bridge.
    """
    cur: Any = blob
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _read_url(decl: MCPServerDecl) -> str | None:
    """Read the configured URL out of the live settings file.

    Returns None when the user hasn't filled the field in. Callers
    must handle that case (the synthesised tool handler does, returning
    an ``mcp_not_configured`` error).
    """
    try:
        val = _dotted_get(user_settings.read(), decl.url_setting)
    except Exception:
        return None
    return (val or None) if isinstance(val, str) else None


def _read_token(decl: MCPServerDecl) -> str | None:
    """Read the configured bearer token. None means "no auth".

    Local-dev mode on the server side (``VOITTA_SINGLE_USER`` etc.)
    accepts unauthenticated MCP calls, so an empty token is a
    legitimate value here — we don't synthesise a placeholder.
    """
    if not decl.token_setting:
        return None
    try:
        val = _dotted_get(user_settings.read(), decl.token_setting)
    except Exception:
        return None
    return (val or None) if isinstance(val, str) else None


# -- refresh / tool synthesis ------------------------------------------------


def _drop_owned_tools(conn: MCPConnector) -> None:
    """Remove every ToolSpec this connector previously synthesised.

    Iterates the underlying dict directly because ``ToolRegistry``
    today has no remove() — kept the abstraction surface minimal.
    Touching ``_tools`` here is intentional and bounded to this file.
    """
    for name in list(conn.owned_tool_names):
        _tool_registry._tools.pop(name, None)
    conn.owned_tool_names.clear()
    conn.tool_names = []


def _is_exposed(decl: MCPServerDecl, remote_name: str) -> bool:
    if decl.expose_tools is None:
        return True
    return remote_name in decl.expose_tools


def _host_pattern_for(decl: MCPServerDecl) -> str | list[str] | None:
    """Pick the host_pattern shape ToolSpec wants.

    Single-host plugins get a bare string (cheaper to serialise + match
    against). Multi-host plugins get the full list; the registry's
    matcher OR's them.
    """
    pats = [p for p in decl.host_patterns if isinstance(p, str) and p]
    if not pats:
        return None
    return pats[0] if len(pats) == 1 else pats


async def refresh_one(conn: MCPConnector) -> MCPConnector:
    """Probe one connector, replace its synthesised tools.

    On success: tools are listed and a fresh ToolSpec is registered for
    each one. ``status`` becomes ``"ok"`` and ``tool_names`` is filled.

    On failure: the existing synthesised tools are left in place if the
    failure looks transient (network blip); they're cleared if the
    server says the credentials are bad. The status field carries the
    classification so the UI can render an actionable badge.
    """
    decl = conn.decl
    url = _read_url(decl)
    _logger.info("refresh_one: plugin=%s connector=%s url=%r", decl.plugin_name, decl.id, url)
    if not url:
        conn.status = "not_configured"
        conn.last_error = None
        _drop_owned_tools(conn)
        return conn

    token = _read_token(decl)
    try:
        tools = await mcp_client.list_tools(url, token)
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        if _is_auth_error(exc, msg):
            conn.status = "unauth"
            _drop_owned_tools(conn)
        else:
            conn.status = "unreachable"
            # Keep prior tools so a brief outage doesn't pull working
            # tools out from under an in-progress chat. The next
            # successful refresh will replace them.
        conn.last_error = msg
        _logger.warning(
            "mcp refresh failed: plugin=%s connector=%s url=%s err=%s",
            decl.plugin_name, decl.id, url, msg,
        )
        return conn

    # Success — drop the old, register the new.
    _drop_owned_tools(conn)
    url_provider: Callable[[], str | None] = lambda d=decl: _read_url(d)
    token_provider: Callable[[], str | None] = lambda d=decl: _read_token(d)
    host_pattern = _host_pattern_for(decl)

    visibility_check: Callable[[], bool] = lambda c=conn: c.status == "ok"

    registered: list[str] = []
    for t in tools:
        if not _is_exposed(decl, t.name):
            continue
        spec: ToolSpec = synth_tool_spec(
            connector_id=f"{decl.plugin_name}:{decl.id}",
            remote_tool=t,
            tool_prefix=decl.tool_prefix,
            url_provider=url_provider,
            token_provider=token_provider,
            host_pattern=host_pattern,
            visibility_check=visibility_check,
        )
        # Tag ownership so user-added activation hosts
        # (plugins.<name>.extra_hosts) can widen the host gate.
        spec.plugin_name = decl.plugin_name
        try:
            _tool_registry.register(spec)
        except ValueError:
            # Name collision with a local tool of the same name, or with
            # another plugin using an overlapping prefix. We log and skip
            # so the rest of the connector's tools still surface.
            _logger.warning(
                "mcp tool name collision: connector=%s name=%s — skipped",
                decl.id, spec.name,
            )
            continue
        registered.append(spec.name)

    conn.owned_tool_names = set(registered)
    conn.tool_names = registered
    conn.status = "ok"
    conn.last_error = None
    _logger.info(
        "mcp refresh ok: plugin=%s connector=%s host_pattern=%r tools=%d: %s",
        decl.plugin_name, decl.id, host_pattern, len(registered), registered,
    )
    return conn


async def refresh_all() -> list[MCPConnector]:
    """Probe every registered connector. Errors are absorbed per-connector.

    Returns the connector list in registration order so callers can
    surface per-connector status in the UI.
    """
    out: list[MCPConnector] = []
    for conn in MCP_CONNECTORS.values():
        out.append(await refresh_one(conn))
    return out


def _is_auth_error(exc: Exception, msg: str) -> bool:
    """Heuristic: does this exception look like a 401/403?

    fastmcp / httpx surface HTTP errors with the status code in the
    string but no easily-introspectable status attribute. A substring
    match is good enough for status-badge purposes; the precise error
    text always shows in the UI regardless of classification.
    """
    msg = msg.lower()
    if "401" in msg or "unauthorized" in msg:
        return True
    if "403" in msg or "forbidden" in msg:
        return True
    return False
