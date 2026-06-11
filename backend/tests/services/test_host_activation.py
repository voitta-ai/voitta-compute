"""User-added activation hosts (``plugins.<name>.extra_hosts``).

Covers the port-aware matcher, the settings-blob parser, and the two
consumers: ``ToolRegistry.visible_for_host`` and ``plugins.for_host``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import host_activation
from app.tools.registry import ToolCtx, ToolRegistry, ToolSpec


async def _noop(_args: dict, _ctx: ToolCtx) -> dict:
    return {}


def _settings(monkeypatch: pytest.MonkeyPatch, blob: dict) -> None:
    monkeypatch.setattr(host_activation.user_settings, "read", lambda: blob)


# -- matches() ----------------------------------------------------------------


def test_matches_bare_hostname_exact_and_suffix() -> None:
    assert host_activation.matches("rag.corp.example", ["corp.example"])
    assert host_activation.matches("corp.example", ["corp.example"])
    assert not host_activation.matches("corp.example.evil.com", ["corp.example"])


def test_matches_entry_with_port_requires_port() -> None:
    assert host_activation.matches("127.0.0.1:8756", ["127.0.0.1:8756"])
    assert not host_activation.matches("127.0.0.1:9999", ["127.0.0.1:8756"])
    assert not host_activation.matches("127.0.0.1", ["127.0.0.1:8756"])


def test_matches_entry_without_port_ignores_page_port() -> None:
    assert host_activation.matches("127.0.0.1:8756", ["127.0.0.1"])


def test_matches_empty_inputs() -> None:
    assert not host_activation.matches(None, ["x.com"])
    assert not host_activation.matches("", ["x.com"])
    assert not host_activation.matches("x.com", [])


# -- extra_hosts_map() --------------------------------------------------------


def test_extra_hosts_map_parses_and_normalises(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {
        "plugins": {
            "voitta-enterprise": {"extra_hosts": [" 127.0.0.1:8756 ", "RAG.Corp.Example"]},
            "ebay": {"mcp": {"url": "x"}},          # no extra_hosts key
            "bad": {"extra_hosts": "not-a-list"},   # wrong type → skipped
        }
    })
    assert host_activation.extra_hosts_map() == {
        "voitta-enterprise": ["127.0.0.1:8756", "rag.corp.example"],
    }


def test_extra_hosts_map_tolerates_unreadable_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> dict:
        raise OSError("nope")

    monkeypatch.setattr(host_activation.user_settings, "read", boom)
    assert host_activation.extra_hosts_map() == {}


# -- visible_for_host ---------------------------------------------------------


def _gated_spec(name: str, plugin_name: str | None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {}},
        handler=_noop,
        host_pattern="enterprise.voitta.ai",
        plugin_name=plugin_name,
    )


def test_extra_host_widens_tool_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    reg = ToolRegistry()
    reg.register(_gated_spec("vre_search", "voitta-enterprise"))

    assert [t["name"] for t in reg.schemas_for_host("127.0.0.1:8756")] == ["vre_search"]
    # Wrong port stays hidden; manifest host still works.
    assert reg.schemas_for_host("127.0.0.1:9999") == []
    assert [t["name"] for t in reg.schemas_for_host("enterprise.voitta.ai")] == ["vre_search"]


def test_extra_host_ignored_without_plugin_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    reg = ToolRegistry()
    reg.register(_gated_spec("vre_search", None))
    assert reg.schemas_for_host("127.0.0.1:8756") == []


# -- plugins.for_host ---------------------------------------------------------


def test_for_host_includes_plugin_via_extra_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import plugins as plugins_mod

    _settings(monkeypatch, {
        "plugins": {"voitta-enterprise": {"extra_hosts": ["127.0.0.1:8756"]}}
    })
    plugin = plugins_mod.Plugin(
        name="voitta-enterprise",
        host_patterns=("enterprise.voitta.ai",),
        system_prompt=None,
        python_module=None,
        frontend_bundle=None,
        dir=tmp_path,
    )
    monkeypatch.setattr(plugins_mod, "_PLUGINS", [plugin])

    assert [p.name for p in plugins_mod.for_host("127.0.0.1:8756")] == ["voitta-enterprise"]
    assert plugins_mod.for_host("127.0.0.1:9999") == []
    assert [p.name for p in plugins_mod.for_host("enterprise.voitta.ai")] == ["voitta-enterprise"]
