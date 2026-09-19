"""Manifest-declared fixed MCP endpoints (``url: "https://vendor/mcp"``).

For connectors that talk to one vendor-hosted server rather than a
per-instance deployment (Runpod's https://mcp.getrunpod.io/). Neither of the
other two URL sources fits: ``url_template`` must contain ``{host}``, and
``url_setting`` would make the user type a URL that never varies.
"""

from __future__ import annotations

import pytest

from app.services.mcp import registry as mcp_registry
from app.services.mcp.registry import (
    MCPConnector,
    MCPServerDecl,
    _candidate_urls,
    endpoint_for,
)

FIXED = "https://mcp.getrunpod.io/"


def _decl(**over) -> MCPServerDecl:
    base = dict(
        plugin_name="runpod",
        id="api",
        url_setting=None,
        token_setting="plugins.runpod.api.api_key",
        tool_prefix="runpod_",
        expose_tools=None,
        host_patterns=["runpod.io", "console.runpod.io"],
        url=FIXED,
    )
    base.update(over)
    return MCPServerDecl(**base)


def _settings(monkeypatch: pytest.MonkeyPatch, blob: dict) -> None:
    monkeypatch.setattr(mcp_registry.user_settings, "read", lambda: blob)


# -- from_manifest_entry ------------------------------------------------------

def test_manifest_accepts_fixed_url() -> None:
    decl = MCPServerDecl.from_manifest_entry(
        plugin_name="p", host_patterns=["x.com"], raw={"id": "c", "url": FIXED}
    )
    assert decl.url == FIXED
    assert decl.url_template is None and decl.url_setting is None


def test_manifest_rejects_relative_url() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        MCPServerDecl.from_manifest_entry(
            plugin_name="p", host_patterns=["x.com"], raw={"id": "c", "url": "mcp.x.io/"}
        )


def test_manifest_still_requires_some_url_source() -> None:
    with pytest.raises(ValueError, match="needs 'url'"):
        MCPServerDecl.from_manifest_entry(
            plugin_name="p", host_patterns=["x.com"], raw={"id": "c"}
        )


def test_url_template_still_requires_host_placeholder() -> None:
    """The fixed-url addition must not loosen the template rule."""
    with pytest.raises(ValueError, match=r"must contain '\{host\}'"):
        MCPServerDecl.from_manifest_entry(
            plugin_name="p",
            host_patterns=["x.com"],
            raw={"id": "c", "url_template": "https://no-placeholder/mcp"},
        )


# -- endpoint resolution ------------------------------------------------------

def test_candidate_urls_is_just_the_fixed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {})
    assert _candidate_urls(_decl()) == [FIXED]


def test_endpoint_for_ignores_page_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed endpoint must not follow the page the user happens to be on."""
    _settings(monkeypatch, {})
    conn = MCPConnector(decl=_decl())
    assert endpoint_for(conn, "console.runpod.io") == FIXED
    assert endpoint_for(conn, "somewhere.else.com") == FIXED
    assert endpoint_for(conn, None) == FIXED


def test_user_url_setting_overrides_fixed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit settings URL still wins, so the endpoint stays overridable."""
    _settings(monkeypatch, {"plugins": {"runpod": {"api": {"url": "https://staging/mcp"}}}})
    decl = _decl(url_setting="plugins.runpod.api.url")
    assert endpoint_for(MCPConnector(decl=decl), None) == "https://staging/mcp"
    assert _candidate_urls(decl) == ["https://staging/mcp", FIXED]


def test_unset_url_setting_falls_back_to_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {})
    decl = _decl(url_setting="plugins.runpod.api.url")
    assert endpoint_for(MCPConnector(decl=decl), None) == FIXED
