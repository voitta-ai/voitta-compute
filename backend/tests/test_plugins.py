"""Plugin loader tests.

Each test isolates ``PLUGINS_DIR`` to a temp dir and resets the
plugin + tool registries so nothing leaks across tests.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from app import plugins as plugins_mod
from app.tools import registry as registry_mod


@pytest.fixture
def isolated_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Swap PLUGINS_DIR for a tmp tree; clear loaded-plugin state."""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugins_mod, "PLUGINS_DIR", root)
    monkeypatch.setattr(plugins_mod, "_PLUGINS", [])
    return root


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Replace the registry singleton with a fresh one per test."""
    fresh = registry_mod.ToolRegistry()
    monkeypatch.setattr(registry_mod, "registry", fresh)
    return fresh


def _write_plugin(root: Path, name: str, manifest: dict, **files: str) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest))
    for rel, contents in files.items():
        path = plugin_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    return plugin_dir


def test_load_all_reads_system_prompt(isolated_plugins: Path) -> None:
    _write_plugin(
        isolated_plugins,
        "alpha",
        {"name": "alpha", "host_patterns": ["*"], "system_prompt": "p.md"},
        **{"p.md": "ALPHA PROMPT"},
    )
    loaded = plugins_mod.load_all()
    assert len(loaded) == 1
    assert loaded[0].name == "alpha"
    assert loaded[0].system_prompt == "ALPHA PROMPT"


def test_for_host_matches_wildcard(isolated_plugins: Path) -> None:
    _write_plugin(
        isolated_plugins,
        "always",
        {"name": "always", "host_patterns": ["*"]},
    )
    plugins_mod.load_all()
    assert [p.name for p in plugins_mod.for_host("anything.example")] == ["always"]
    assert [p.name for p in plugins_mod.for_host(None)] == ["always"]


def test_for_host_suffix_match(isolated_plugins: Path) -> None:
    _write_plugin(
        isolated_plugins,
        "ebay",
        {"name": "ebay", "host_patterns": ["ebay.com"]},
    )
    plugins_mod.load_all()
    assert [p.name for p in plugins_mod.for_host("www.ebay.com")] == ["ebay"]
    assert plugins_mod.for_host("drive.google.com") == []


def test_python_module_imports_and_registers_tools(
    isolated_plugins: Path, isolated_registry: dict
) -> None:
    """A plugin with ``python_module`` gets imported, its tools land in
    the registry, and the manifest's ``host_patterns`` are back-filled
    onto tools that didn't declare their own."""
    code = textwrap.dedent(
        """
        from app.tools.registry import ToolCtx, ToolSpec, registry

        async def _handler(args, _ctx: ToolCtx):
            return {"ok": True, "got": args}

        registry.register(ToolSpec(
            name="alpha_ping",
            description="ping",
            input_schema={"type": "object", "properties": {}},
            handler=_handler,
            side="server",
        ))
        """
    )
    _write_plugin(
        isolated_plugins,
        "alpha",
        {
            "name": "alpha",
            "host_patterns": ["alpha.example"],
            "python_module": "voitta_alpha",
        },
        **{"backend/voitta_alpha/__init__.py": code},
    )
    plugins_mod.load_all()
    assert "alpha_ping" in isolated_registry.names()
    spec = isolated_registry.get("alpha_ping")
    assert spec is not None
    assert spec.host_pattern == "alpha.example"


def test_per_tool_host_pattern_wins_over_manifest(
    isolated_plugins: Path, isolated_registry: dict
) -> None:
    """Tools that set their own ``host_pattern`` are not overridden."""
    code = textwrap.dedent(
        """
        from app.tools.registry import ToolCtx, ToolSpec, registry

        async def _handler(args, _ctx: ToolCtx):
            return {}

        registry.register(ToolSpec(
            name="beta_tool",
            description="d",
            input_schema={"type": "object", "properties": {}},
            handler=_handler,
            side="server",
            host_pattern="specific.example",
        ))
        """
    )
    _write_plugin(
        isolated_plugins,
        "beta",
        {
            "name": "beta",
            "host_patterns": ["broad.example"],
            "python_module": "voitta_beta",
        },
        **{"backend/voitta_beta/__init__.py": code},
    )
    plugins_mod.load_all()
    spec = isolated_registry.get("beta_tool")
    assert spec is not None
    assert spec.host_pattern == "specific.example"


def test_missing_frontend_bundle_logs_but_loads(
    isolated_plugins: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_plugin(
        isolated_plugins,
        "gamma",
        {
            "name": "gamma",
            "host_patterns": ["*"],
            "frontend_bundle": "frontend/widget.ts",
        },
    )
    with caplog.at_level("WARNING", logger="app.plugins"):
        loaded = plugins_mod.load_all()
    assert len(loaded) == 1
    assert any("frontend_bundle" in r.message for r in caplog.records)


def test_bad_python_module_does_not_crash_load(
    isolated_plugins: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_plugin(
        isolated_plugins,
        "broken",
        {
            "name": "broken",
            "host_patterns": ["*"],
            "python_module": "voitta_broken",
        },
        **{"backend/voitta_broken/__init__.py": "raise RuntimeError('boom')\n"},
    )
    with caplog.at_level("ERROR", logger="app.plugins"):
        loaded = plugins_mod.load_all()
    assert len(loaded) == 1  # plugin row still recorded
    assert any("import voitta_broken failed" in r.message for r in caplog.records)


def test_default_plugin_present_in_repo() -> None:
    """The shipped default plugin lives at top-level ``plugins/default``
    and provides the base Voitta system prompt."""
    from app.config import PLUGINS_DIR

    default_dir = PLUGINS_DIR / "default"
    assert default_dir.is_dir(), f"missing {default_dir}"
    manifest = json.loads((default_dir / "manifest.json").read_text())
    assert manifest["host_patterns"] == ["*"]
    sp = (default_dir / manifest["system_prompt"]).read_text()
    assert "You are Voitta" in sp
