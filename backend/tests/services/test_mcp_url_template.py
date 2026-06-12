"""Page-host-derived MCP endpoints (``url_template: "{host}/mcp"``)."""

from __future__ import annotations

import pytest

from app.services.mcp import registry as mcp_registry
from app.services.mcp.registry import (
    MCPConnector,
    MCPServerDecl,
    _candidate_urls,
    _fill_template,
    endpoint_for,
    token_for,
)


def _decl(**over) -> MCPServerDecl:
    base = dict(
        plugin_name="voitta-enterprise",
        id="vre",
        url_setting=None,
        token_setting=None,
        tool_prefix="vre_",
        expose_tools=None,
        host_patterns=["enterprise.voitta.ai"],
        url_template="{host}/mcp",
    )
    base.update(over)
    return MCPServerDecl(**base)


def _settings(monkeypatch: pytest.MonkeyPatch, blob: dict) -> None:
    monkeypatch.setattr(mcp_registry.user_settings, "read", lambda: blob)


# -- from_manifest_entry ------------------------------------------------------


def test_manifest_accepts_url_template() -> None:
    decl = MCPServerDecl.from_manifest_entry(
        plugin_name="p",
        host_patterns=["x.com"],
        raw={"id": "c", "url_template": "{host}/mcp"},
    )
    assert decl.url_template == "{host}/mcp"
    assert decl.url_setting is None


def test_manifest_requires_url_setting_or_template() -> None:
    with pytest.raises(ValueError, match="url_setting.*url_template"):
        MCPServerDecl.from_manifest_entry(
            plugin_name="p", host_patterns=[], raw={"id": "c"}
        )


def test_manifest_template_must_carry_host_placeholder() -> None:
    with pytest.raises(ValueError, match="\\{host\\}"):
        MCPServerDecl.from_manifest_entry(
            plugin_name="p", host_patterns=[], raw={"id": "c", "url_template": "/mcp"}
        )


# -- _fill_template -----------------------------------------------------------


def test_fill_template_https_for_public_hosts() -> None:
    assert _fill_template("{host}/mcp", "enterprise.voitta.ai") == (
        "https://enterprise.voitta.ai/mcp"
    )


def test_fill_template_http_for_loopback() -> None:
    assert _fill_template("{host}/mcp", "127.0.0.1:8756") == "http://127.0.0.1:8756/mcp"
    assert _fill_template("{host}/mcp", "localhost:8000") == "http://localhost:8000/mcp"


def test_fill_template_explicit_scheme_verbatim() -> None:
    assert _fill_template("https://{host}/mcp", "127.0.0.1:8756") == (
        "https://127.0.0.1:8756/mcp"
    )


# -- endpoint_for precedence --------------------------------------------------


def test_endpoint_explicit_setting_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    decl = _decl(url_setting="plugins.p.mcp.url")
    _settings(monkeypatch, {"plugins": {"p": {"mcp": {"url": "https://pinned/mcp"}}}})
    conn = MCPConnector(decl=decl, active_url="https://stale/mcp")
    assert endpoint_for(conn, "127.0.0.1:8756") == "https://pinned/mcp"


def test_endpoint_template_follows_page_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {})
    conn = MCPConnector(decl=_decl(), active_url="https://enterprise.voitta.ai/mcp")
    assert endpoint_for(conn, "127.0.0.1:8756") == "http://127.0.0.1:8756/mcp"
    assert endpoint_for(conn, "enterprise.voitta.ai") == "https://enterprise.voitta.ai/mcp"


def test_endpoint_hostless_falls_back_to_active_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {})
    conn = MCPConnector(decl=_decl(), active_url="http://127.0.0.1:8756/mcp")
    assert endpoint_for(conn, None) == "http://127.0.0.1:8756/mcp"


# -- _candidate_urls ----------------------------------------------------------


def test_candidates_patterns_then_extra_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    assert _candidate_urls(_decl()) == [
        "https://enterprise.voitta.ai/mcp",
        "http://127.0.0.1:8756/mcp",
    ]


def test_candidates_skip_wildcard_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {})
    assert _candidate_urls(_decl(host_patterns=["*", "*.voitta.ai"])) == []


# -- token_for (per-host keys) ------------------------------------------------

_KEYED = dict(
    token_setting="plugins.voitta-enterprise.mcp.api_key",
    token_map_setting="plugins.voitta-enterprise.mcp.api_keys",
)


def test_token_matched_to_called_url_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"mcp": {"api_keys": {
            "enterprise.voitta.ai": "vk_prod",
            "127.0.0.1:8756": "vk_local",
        }}}}
    })
    decl = _decl(**_KEYED)
    assert token_for(decl, "https://enterprise.voitta.ai/mcp") == "vk_prod"
    assert token_for(decl, "http://127.0.0.1:8756/mcp") == "vk_local"
    # Unknown host, no legacy key → unauthenticated.
    assert token_for(decl, "http://127.0.0.1:9999/mcp") is None


def test_token_bare_hostname_entry_suffix_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"mcp": {"api_keys": {
            "voitta.ai": "vk_any",
        }}}}
    })
    assert token_for(_decl(**_KEYED), "https://enterprise.voitta.ai/mcp") == "vk_any"


def test_token_falls_back_to_legacy_single_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"mcp": {"api_key": "vk_legacy"}}}
    })
    decl = _decl(**_KEYED)
    assert token_for(decl, "http://127.0.0.1:8756/mcp") == "vk_legacy"
    assert token_for(decl, None) == "vk_legacy"


# -- refresh_one: per-endpoint probing ----------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.inputSchema = {"type": "object", "properties": {}}


def _fake_list_tools(by_url: dict[str, object]):
    """``{url: [tools] | Exception}`` → an async list_tools stand-in."""
    async def _impl(url: str, token: str | None):
        out = by_url[url]
        if isinstance(out, Exception):
            raise out
        return out
    return _impl


async def test_refresh_probes_every_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": [_FakeTool("search")],
            "http://127.0.0.1:8756/mcp": [_FakeTool("search"), _FakeTool("get_file")],
        }),
    )
    conn = MCPConnector(decl=_decl())
    await mcp_registry.refresh_one(conn)

    assert conn.status == "ok"
    assert [(e.url, e.status) for e in conn.endpoints] == [
        ("https://enterprise.voitta.ai/mcp", "ok"),
        ("http://127.0.0.1:8756/mcp", "ok"),
    ]
    assert conn.endpoints[1].tool_names == ["vre_get_file", "vre_search"]
    # Merged registration: union of both instances' tools.
    assert sorted(conn.tool_names) == ["vre_get_file", "vre_search"]
    assert conn.active_url == "https://enterprise.voitta.ai/mcp"
    mcp_registry._drop_owned_tools(conn)  # clean the global registry


async def test_refresh_partial_outage_still_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": ConnectionError("refused"),
            "http://127.0.0.1:8756/mcp": [_FakeTool("search")],
        }),
    )
    conn = MCPConnector(decl=_decl())
    await mcp_registry.refresh_one(conn)

    assert conn.status == "ok"
    assert conn.endpoints[0].status == "unreachable"
    assert conn.endpoints[1].status == "ok"
    # Fallback URL is the first REACHABLE endpoint.
    assert conn.active_url == "http://127.0.0.1:8756/mcp"
    assert conn.tool_names == ["vre_search"]
    mcp_registry._drop_owned_tools(conn)


async def test_refresh_endpoint_touches_only_that_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": [_FakeTool("search")],
            "http://127.0.0.1:8756/mcp": ConnectionError("down"),
        }),
    )
    conn = MCPConnector(decl=_decl())
    await mcp_registry.refresh_one(conn)
    assert [e.status for e in conn.endpoints] == ["ok", "unreachable"]

    # Local instance comes up; re-probe ONLY it.
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": AssertionError("must not re-probe"),
            "http://127.0.0.1:8756/mcp": [_FakeTool("search"), _FakeTool("get_file")],
        }),
    )
    await mcp_registry.refresh_endpoint(conn, "127.0.0.1:8756")

    assert [(e.url, e.status) for e in conn.endpoints] == [
        ("https://enterprise.voitta.ai/mcp", "ok"),       # untouched
        ("http://127.0.0.1:8756/mcp", "ok"),              # re-probed
    ]
    # Union re-merged across BOTH endpoints' cached tool lists.
    assert sorted(conn.tool_names) == ["vre_get_file", "vre_search"]
    assert conn.status == "ok"
    mcp_registry._drop_owned_tools(conn)


async def test_refresh_endpoint_no_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated single-host refreshes replace the row, never append."""
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": [_FakeTool("search")],
            "http://127.0.0.1:8756/mcp": [_FakeTool("search")],
        }),
    )
    conn = MCPConnector(decl=_decl())
    await mcp_registry.refresh_one(conn)
    await mcp_registry.refresh_endpoint(conn, "127.0.0.1:8756")
    await mcp_registry.refresh_endpoint(conn, "127.0.0.1:8756")

    urls = [e.url for e in conn.endpoints]
    assert urls == sorted(set(urls), key=urls.index)  # no dupes, order kept
    assert len(urls) == 2
    mcp_registry._drop_owned_tools(conn)


async def test_refresh_all_down_keeps_prior_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {})
    monkeypatch.setattr(
        mcp_registry.mcp_client, "list_tools",
        _fake_list_tools({
            "https://enterprise.voitta.ai/mcp": ConnectionError("refused"),
        }),
    )
    conn = MCPConnector(decl=_decl())
    conn.tool_names = ["vre_search"]  # from a prior good refresh
    await mcp_registry.refresh_one(conn)

    assert conn.status == "unreachable"
    assert conn.tool_names == ["vre_search"]  # transient blip: kept
    assert "refused" in (conn.last_error or "")
