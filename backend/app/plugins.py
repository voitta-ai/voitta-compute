"""Plugin loader (BE-side).

Discovers ``plugins/**/manifest.json`` at startup (recursive, so nested
plugin trees like ``plugins/google/drive/`` work). A manifest looks like:

    {
      "name": "ebay",
      "host_patterns": ["ebay.com"],
      "python_module": "voitta_ebay",
      "frontend_bundle": "frontend/widget.ts",
      "system_prompt": "prompt.md"
    }

``host_patterns`` is a list of glob-ish strings matched against the
page's host. ``"*"`` matches every page (the default plugin uses this
to always contribute the base Voitta prompt). ``system_prompt`` is a
path relative to the manifest file; its contents get appended to the
system prompt when the plugin applies.

If ``python_module`` is set, the plugin's ``backend/`` dir is added to
``sys.path`` and the named package is imported — its
``register(ToolSpec(...))`` calls run as import side effects. Any
ToolSpec the plugin contributes that didn't declare its own
``host_pattern`` gets back-filled with the manifest's ``host_patterns``
so authors specify host gating ONCE in the manifest.

``frontend_bundle`` is informational on the BE — it's validated to
exist (warn if not), but actual FE loading happens via the Vite glob
in ``frontend/src/widget.tsx``. ``mcp_servers`` is reserved for a
future MCP layer; entries are skipped with a log line today.
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import PLUGINS_DIR

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plugin:
    name: str
    host_patterns: tuple[str, ...]
    system_prompt: str | None  # already-loaded file contents, not a path
    python_module: str | None
    frontend_bundle: str | None
    dir: Path
    rel_dir: str = ""  # path relative to PLUGINS_DIR, e.g. "google/drive"
    # Manifest metadata surfaced to the Settings UI via /api/plugins.
    version: str | None = None
    description: str | None = None
    agent_name: str | None = None
    settings_schema: dict | None = None
    # "schema" | "custom" — drives whether the FE looks for a
    # settings-panel.tsx in the plugin's frontend/ dir.
    settings_panel: str = "schema"


_PLUGINS: list[Plugin] = []


def _back_fill_host_patterns(
    plugin_name: str, host_patterns: tuple[str, ...], before: set[str]
) -> None:
    """Apply manifest host_patterns to ToolSpecs the plugin just added.

    Snapshot of tool names BEFORE the plugin import tells us which
    specs are newly contributed. Per-tool ``host_pattern`` overrides
    win — only specs that left the field ``None`` get the manifest
    default. Every new spec (global tools excepted) also gets tagged
    with its owning ``plugin_name`` so user-added activation hosts can
    widen the gate at match time.
    """
    from app.tools.registry import registry

    added_keys = [n for n in registry.names() if n not in before]
    applied = 0
    for name in added_keys:
        spec = registry.get(name)
        if spec is None:
            continue
        if spec.plugin_name is None and not spec.global_tool:
            spec.plugin_name = plugin_name
        if host_patterns and spec.host_pattern is None:
            spec.host_pattern = (
                host_patterns[0]
                if len(host_patterns) == 1
                else list(host_patterns)
            )
            applied += 1
    if added_keys:
        logger.info(
            "plugin %s: %d tools registered, host_patterns=%r applied to %d unset",
            plugin_name, len(added_keys), list(host_patterns), applied,
        )


def _import_python_module(plugin_dir: Path, manifest: dict) -> None:
    """Import the plugin's Python package so its ToolSpecs register.

    Failure is logged but doesn't kill startup — a bad plugin shouldn't
    take the whole backend down. The user sees a missing-tools symptom
    instead, with a clear log line pointing at the cause.
    """
    package_name = manifest.get("python_module")
    if not isinstance(package_name, str) or not package_name:
        return
    backend_dir = plugin_dir / "backend"
    if not backend_dir.is_dir():
        logger.warning(
            "plugin %s: manifest.python_module=%r but no backend/ dir at %s",
            plugin_dir.name, package_name, backend_dir,
        )
        return
    sys_path_entry = str(backend_dir)
    if sys_path_entry not in sys.path:
        sys.path.insert(0, sys_path_entry)

    from app.tools.registry import registry

    before = set(registry.names())
    try:
        importlib.import_module(package_name)
    except Exception:
        logger.exception(
            "plugin %s: import %s failed", plugin_dir.name, package_name
        )
        return

    raw_patterns = manifest.get("host_patterns") or []
    if isinstance(raw_patterns, str):
        raw_patterns = [raw_patterns]
    patterns = tuple(p for p in raw_patterns if isinstance(p, str) and p)
    # Tag with the manifest name (not the dir name) — extra_hosts
    # settings are keyed by manifest name.
    plugin_name = manifest.get("name") or plugin_dir.name
    _back_fill_host_patterns(plugin_name, patterns, before)


def _warn_if_missing_frontend_bundle(plugin_dir: Path, manifest: dict) -> None:
    rel = manifest.get("frontend_bundle")
    if not isinstance(rel, str) or not rel:
        return
    if not (plugin_dir / rel).is_file():
        logger.warning(
            "plugin %s: manifest.frontend_bundle=%r not found at %s — "
            "browser primitives from this plugin will not load",
            plugin_dir.name, rel, plugin_dir / rel,
        )


def _register_mcp_servers(plugin_name: str, host_patterns: list[str], manifest: dict) -> None:
    """Materialise ``mcp_servers[]`` declarations as MCP connectors.

    No network at this stage — :func:`register_connector` just records
    the declaration. The first ``refresh_all()`` call (FastAPI startup
    or explicit ``/api/plugins/<name>/refresh``) opens connections and
    synthesises ToolSpecs.
    """
    decls = manifest.get("mcp_servers")
    if not decls:
        return
    if not isinstance(decls, list):
        logger.warning("plugin %s: mcp_servers must be a list; got %r", plugin_name, type(decls))
        return
    # Late import: ``app.services.mcp.registry`` imports the tool
    # registry, which is fine but we avoid hoisting the cost into every
    # ``load_all`` invocation.
    from app.services.mcp.registry import MCPServerDecl, register_connector

    for entry in decls:
        try:
            decl = MCPServerDecl.from_manifest_entry(
                plugin_name=plugin_name,
                host_patterns=list(host_patterns),
                raw=entry,
            )
        except Exception:
            logger.exception(
                "plugin %s: bad mcp_servers entry %r; skipping", plugin_name, entry
            )
            continue
        register_connector(decl)
        logger.info(
            "plugin %s: registered MCP connector %r (url_setting=%s)",
            plugin_name, decl.id, decl.url_setting,
        )


def load_all() -> list[Plugin]:
    """Re-scan ``PLUGINS_DIR`` and refresh the in-memory list."""
    _PLUGINS.clear()
    if not PLUGINS_DIR.exists():
        logger.info("plugins dir %s does not exist; skipping", PLUGINS_DIR)
        return []
    for manifest_path in sorted(PLUGINS_DIR.glob("**/manifest.json")):
        plugin_dir = manifest_path.parent
        try:
            data = json.loads(manifest_path.read_text())
        except Exception:
            logger.exception("bad manifest at %s", manifest_path)
            continue
        sp_rel = data.get("system_prompt")
        sp_text: str | None = None
        if sp_rel:
            sp_path = plugin_dir / sp_rel
            try:
                sp_text = sp_path.read_text()
            except Exception:
                logger.exception("system_prompt %s unreadable", sp_path)
        python_module = data.get("python_module") if isinstance(
            data.get("python_module"), str
        ) else None
        frontend_bundle = data.get("frontend_bundle") if isinstance(
            data.get("frontend_bundle"), str
        ) else None
        raw_hp = data.get("host_patterns") or ["*"]
        if isinstance(raw_hp, str):
            raw_hp = [raw_hp]
        host_patterns = tuple(p for p in raw_hp if isinstance(p, str) and p) or ("*",)
        _import_python_module(plugin_dir, data)
        _warn_if_missing_frontend_bundle(plugin_dir, data)
        _register_mcp_servers(data.get("name") or plugin_dir.name, list(host_patterns), data)
        settings_panel = data.get("settings_panel")
        if not isinstance(settings_panel, str) or settings_panel not in ("schema", "custom"):
            settings_panel = "schema"
        try:
            rel_dir = str(plugin_dir.relative_to(PLUGINS_DIR))
        except ValueError:
            rel_dir = plugin_dir.name
        _PLUGINS.append(
            Plugin(
                name=data["name"],
                host_patterns=host_patterns,
                system_prompt=sp_text,
                python_module=python_module,
                frontend_bundle=frontend_bundle,
                dir=plugin_dir,
                rel_dir=rel_dir,
                version=data.get("version") if isinstance(data.get("version"), str) else None,
                description=data.get("description") if isinstance(data.get("description"), str) else None,
                agent_name=data.get("agent_name") if isinstance(data.get("agent_name"), str) else None,
                settings_schema=data.get("settings_schema") if isinstance(data.get("settings_schema"), dict) else None,
                settings_panel=settings_panel,
            )
        )
    logger.info("loaded %d plugins: %s", len(_PLUGINS), [p.name for p in _PLUGINS])
    return list(_PLUGINS)


def for_host(host: str | None) -> list[Plugin]:
    """All plugins whose host_patterns match ``host``.

    Matching: ``"*"`` always matches; otherwise ``fnmatch`` against the
    bare hostname (port stripped, lowercased). Hostnames are also
    suffix-matched so ``"ebay.com"`` matches ``"www.ebay.com"``.
    User-added activation hosts (Settings → Plugins, stored at
    ``plugins.<name>.extra_hosts``) are OR'd in, port-aware — see
    :mod:`app.services.host_activation`.
    """
    from app.services import host_activation

    hostname = (host or "").split(":", 1)[0].lower().rstrip(".")
    extra_map = host_activation.extra_hosts_map()
    out: list[Plugin] = []
    for p in _PLUGINS:
        matched = False
        for pat in p.host_patterns:
            if pat == "*":
                matched = True
                break
            pat_norm = pat.lower().rstrip(".")
            if hostname == pat_norm or hostname.endswith("." + pat_norm):
                matched = True
                break
            if fnmatch.fnmatch(hostname, pat_norm):
                matched = True
                break
        if not matched:
            matched = host_activation.matches(host, extra_map.get(p.name, []))
        if matched:
            out.append(p)
    return out


def all_plugins() -> list[Plugin]:
    return list(_PLUGINS)
