"""MCP service — stubs.

The real implementation discovers MCP server declarations from plugin
manifests, opens connections, and synthesises one ToolSpec per remote
tool. Not ported yet. Plugins that import :mod:`app.services.mcp.client`
or :mod:`app.services.mcp.registry` still load; runtime calls into the
client raise ``NotConfigured`` until the full implementation lands.
"""
