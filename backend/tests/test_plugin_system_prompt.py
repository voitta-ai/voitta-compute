"""Per-plugin system-prompt addendum: loader + host-scoped append.

Plugins can ship a ``system_prompt`` file (declared in their
``manifest.json``). The loader reads it once at discovery and stores
it on the plugin record; the chat route appends it to the outgoing
system prompt only when the user's page host matches the plugin's
``host_patterns``. These tests pin both halves of that contract.

We don't spin up FastAPI here — the chat-route integration is
covered by manually invoking the helper. The point is: prompt content
must NOT leak across hosts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_plugin(tmp_path: Path, name: str, manifest: dict, prompt_text: str | None = None) -> Path:
    """Build a fixture plugin directory under ``tmp_path``."""
    p = tmp_path / name
    p.mkdir()
    (p / "manifest.json").write_text(json.dumps(manifest))
    if prompt_text is not None:
        path = manifest.get("system_prompt", "prompt.md")
        (p / path).write_text(prompt_text)
    return p


def test_load_system_prompt_reads_file(tmp_path):
    from app.tools.providers import _load_system_prompt

    body = "# host rules\n\nDo the thing."
    plugin_dir = _make_plugin(
        tmp_path, "fixture-a",
        {"name": "fixture-a", "system_prompt": "prompt.md"},
        prompt_text=body,
    )
    text = _load_system_prompt(plugin_dir, json.loads((plugin_dir / "manifest.json").read_text()))
    assert text == body


def test_load_system_prompt_absent_field_returns_none(tmp_path):
    from app.tools.providers import _load_system_prompt

    plugin_dir = _make_plugin(tmp_path, "fixture-b", {"name": "fixture-b"})
    assert _load_system_prompt(plugin_dir, {"name": "fixture-b"}) is None


def test_load_system_prompt_missing_file_warns_returns_none(tmp_path, caplog):
    from app.tools.providers import _load_system_prompt

    plugin_dir = _make_plugin(
        tmp_path, "fixture-c",
        {"name": "fixture-c", "system_prompt": "nope.md"},
    )
    with caplog.at_level("WARNING"):
        out = _load_system_prompt(
            plugin_dir,
            {"name": "fixture-c", "system_prompt": "nope.md"},
        )
    assert out is None
    assert any("nope.md" in r.message for r in caplog.records)


def test_load_system_prompt_non_string_field_warns(tmp_path, caplog):
    from app.tools.providers import _load_system_prompt

    plugin_dir = _make_plugin(tmp_path, "fixture-d", {"name": "fixture-d"})
    with caplog.at_level("WARNING"):
        out = _load_system_prompt(plugin_dir, {"name": "fixture-d", "system_prompt": 42})
    assert out is None


def test_plugins_for_host_strict_suffix():
    from app.tools.providers import LOADED_PLUGINS, plugins_for_host

    # Seed the module-level list with a synthetic record.
    sentinel = {
        "name": "_test-sentinel",
        "manifest": {"host_patterns": ["sentinel.example"]},
        "path": "/nonexistent",
        "system_prompt": "rule body",
    }
    LOADED_PLUGINS.append(sentinel)
    try:
        # Exact match.
        assert sentinel in plugins_for_host("sentinel.example")
        # Subdomain match (suffix rule).
        assert sentinel in plugins_for_host("sub.sentinel.example")
        # Port stripped.
        assert sentinel in plugins_for_host("sentinel.example:8000")
        # Sibling host does NOT match (avoid accidental prefix-style hits).
        assert sentinel not in plugins_for_host("notsentinel.example")
        # Unrelated host.
        assert sentinel not in plugins_for_host("example.com")
        # Empty / None.
        assert plugins_for_host("") == []
        assert plugins_for_host(None) == []
    finally:
        LOADED_PLUGINS.remove(sentinel)


def test_voitta_enterprise_ships_prompt():
    """The shipped voitta-enterprise plugin must have a loadable
    ``system_prompt`` — this is the canonical example of the new
    mechanism and the migration target for VRE rules removed from
    ``VOITTA_SYSTEM_PROMPT``.
    """
    from app.tools.providers import discover_plugins

    plugins = discover_plugins()
    by_name = {p["name"]: p for p in plugins}
    plugin = by_name.get("voitta-enterprise")
    if plugin is None:
        pytest.skip("voitta-enterprise not present in this checkout")
    assert plugin.get("system_prompt"), (
        "voitta-enterprise/manifest.json declares system_prompt but the "
        "loader didn't pick it up"
    )
    body = plugin["system_prompt"]
    assert "vre_search" in body
    assert "FCStd" in body or ".FCStd" in body


def test_chat_system_assembly_appends_only_for_matching_host(tmp_path):
    """Mirror the assembly logic in ``app.routes.chat`` to make sure
    the host gate actually fires. We don't go through HTTP — we just
    invoke ``plugins_for_host`` the same way the route does and rebuild
    the ``system`` string.
    """
    from app.tools.providers import LOADED_PLUGINS, plugins_for_host

    core = "CORE PROMPT"
    addendum = "## scoped rule"
    sentinel = {
        "name": "_test-host-gate",
        "manifest": {"host_patterns": ["scoped.example"]},
        "path": "/nonexistent",
        "system_prompt": addendum,
    }
    LOADED_PLUGINS.append(sentinel)
    try:
        def assemble(host):
            system = core
            for plugin in plugins_for_host(host):
                a = plugin.get("system_prompt")
                if a:
                    system = system.rstrip() + "\n\n" + a.rstrip()
            return system

        matched = assemble("scoped.example")
        assert addendum in matched
        assert matched.startswith(core)

        unmatched = assemble("other.example")
        assert addendum not in unmatched
        assert unmatched == core

        # None host = no append.
        assert assemble(None) == core
    finally:
        LOADED_PLUGINS.remove(sentinel)
