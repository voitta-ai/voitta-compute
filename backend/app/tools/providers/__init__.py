"""Provider auto-discovery.

Voitta core has no hardcoded providers. Every plugin under ``/plugins/``
that has a ``manifest.json`` and a ``backend/<package>/`` Python module
is loaded at startup; the loader adds the plugin's backend dir to
``sys.path`` and imports its declared package, which calls
``registry.register(...)`` as an import side-effect.

Plugin layout
=============

    plugins/<name>/
    ├── manifest.json           # {name, version, host_patterns, python_module, ...}
    ├── backend/
    │   └── <python_module>/    # importable package
    │       ├── __init__.py     # registers ToolSpecs
    │       └── ...
    ├── frontend/
    │   └── widget.ts           # registers browser primitives
    └── docs/
        └── *.md                # auto-indexed by RAG

The voitta core stays plugin-name-free. ``/plugins/`` is gitignored at
the repo root with a single carve-out for ``/plugins/google/`` so the
canonical OSS reference plugin lives in the tracked tree; all other
plugins (private overlays, anyone else's) stay outside git.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

from app.config import PROJECT_ROOT


_logger = logging.getLogger(__name__)


def _candidate_plugins_dirs() -> list[Path]:
    """Resolve every directory we should scan for plugins.

    Two layouts are supported:

      * Source checkout — ``<repo>/plugins/<name>/`` next to ``backend/``
        and ``frontend/``.
      * Packaged .app — plugin trees staged into the bundle's
        ``Resources/app/plugins/<name>/`` by ``build_app.sh``. The
        helper resolves via importlib because ``__file__`` lands inside
        the bundle's ``app_packages/app/tools/providers/__init__.py``.
    """
    seen: list[Path] = []

    # Layout 1: repo-root /plugins
    here = Path(__file__).resolve()
    repo_root = here.parents[4]
    repo_plugins = repo_root / "plugins"
    if repo_plugins.is_dir():
        seen.append(repo_plugins)

    # Layout 2: alongside the running ``app`` package (briefcase bundle)
    try:
        import app as _app
        app_root = Path(_app.__file__).resolve().parent
        bundled = app_root.parent / "plugins"
        if bundled.is_dir() and bundled not in seen:
            seen.append(bundled)
    except Exception:
        pass

    # Layout 3a: ``src/voitta/resources/plugins/`` inside the briefcase
    # bundle. ``build_app.sh`` stages every ``$ROOT/plugins/*`` here.
    try:
        import voitta as _voitta
        res_plugins = Path(_voitta.__file__).resolve().parent / "resources" / "plugins"
        if res_plugins.is_dir() and res_plugins not in seen:
            seen.append(res_plugins)
    except Exception:
        pass

    # Layout 3: alongside PROJECT_ROOT (user data dir)
    user_plugins = PROJECT_ROOT / "plugins"
    if user_plugins.is_dir() and user_plugins not in seen:
        seen.append(user_plugins)

    return seen


def _load_manifest(plugin_dir: Path) -> dict | None:
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except Exception as exc:
        _logger.warning("plugin %s: bad manifest.json: %s", plugin_dir.name, exc)
        return None


def _import_plugin(plugin_dir: Path, manifest: dict) -> None:
    """Import the plugin's Python package so its ToolSpecs register.

    After import, walk the global registry and back-fill ``host_pattern``
    on any ToolSpec the plugin just added that didn't declare one.
    This lets plugin authors specify host gating ONCE in
    ``manifest.json`` instead of repeating it on every ToolSpec — and
    guarantees the gate exists even if a tool author forgets.

    Failure is logged but doesn't kill startup — a bad plugin shouldn't
    take the whole backend down. The user sees a missing-tools symptom
    instead, with a clear log line pointing at the cause.
    """
    backend_dir = plugin_dir / "backend"
    if not backend_dir.is_dir():
        _logger.info("plugin %s: no backend/ dir, skipping", plugin_dir.name)
        return
    package_name = manifest.get("python_module")
    if not isinstance(package_name, str) or not package_name:
        _logger.warning("plugin %s: manifest.python_module missing", plugin_dir.name)
        return
    sys_path_entry = str(backend_dir)
    if sys_path_entry not in sys.path:
        sys.path.insert(0, sys_path_entry)

    # Snapshot registry state BEFORE importing so we can identify which
    # ToolSpecs the plugin contributed.
    from app.tools.registry import registry as _registry
    before = {t.name for t in _registry.all()}

    try:
        importlib.import_module(package_name)
    except Exception as exc:
        _logger.exception("plugin %s: import %s failed: %s",
                          plugin_dir.name, package_name, exc)
        return

    # Apply manifest host_patterns to plugin-contributed tools that
    # didn't declare their own. Multi-host plugins declare a list of
    # patterns; the FULL list is applied (registry's matcher OR's them
    # together). Tools that need a tighter gate can still override
    # per-ToolSpec, in which case the manifest list is ignored for
    # that tool.
    raw_patterns = manifest.get("host_patterns")
    host_patterns: list[str] = []
    if isinstance(raw_patterns, list):
        host_patterns = [p for p in raw_patterns if isinstance(p, str) and p]
    if host_patterns:
        added = [t for t in _registry.all() if t.name not in before]
        applied = 0
        for spec in added:
            if spec.host_pattern is None:
                # Single string when there's only one host (cheaper to
                # serialise + display); list otherwise.
                spec.host_pattern = (
                    host_patterns[0] if len(host_patterns) == 1
                    else list(host_patterns)
                )
                applied += 1
        if added:
            _logger.info(
                "plugin %s: %d tools registered, host_patterns=%r applied to %d unset",
                plugin_dir.name, len(added), host_patterns, applied,
            )
    else:
        _logger.info("plugin %s: loaded (module=%s)", plugin_dir.name, package_name)


def _discover() -> list[dict]:
    loaded: list[dict] = []
    for plugins_root in _candidate_plugins_dirs():
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            manifest = _load_manifest(child)
            if manifest is None:
                continue
            _import_plugin(child, manifest)
            loaded.append({"name": child.name, "manifest": manifest, "path": str(child)})
    return loaded


# Discovery runs once at import time. The list is held for diagnostic
# routes (Settings panel, /healthz/plugins) so the user can tell what's
# active without reading logs.
LOADED_PLUGINS: list[dict] = _discover()
