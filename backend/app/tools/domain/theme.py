"""Tool: ``get_active_theme`` — expose the active plugin's visual theme.

Reports / charts / Three.js scenes look out of place when they're rendered
in default colours on top of a host page with its own palette. The widget
already solves this for its own shadow DOM: ``frontend/src/theme.css``
holds the canonical token set, and a plugin's ``theme.css`` overrides
specific tokens (see ``docs/INTEGRATION.md`` "Branding and theming").
Reports run *server-side* inside a Bokeh / Panel iframe, so they can't
read those CSS custom properties at runtime. This tool surfaces them in
a form the LLM can paste straight into matplotlib rcParams, Plotly
``layout``, Three.js material configs, or a raw ``<style>`` block.

Resolution algorithm:
  1. Read core defaults from ``frontend/src/theme.css``.
  2. If the caller passed ``host`` and a plugin matches it (same
     ``host_patterns`` suffix-match as the rest of the platform),
     read that plugin's ``theme.css`` and overlay its tokens.
  3. Return everything: categorised palette, raw token map, plugin
     overrides only, plus a ready-to-paste ``:host { … }`` block.

The output is intentionally redundant — different LLM use-cases want
different shapes, and the payload is small enough that returning all
three doesn't matter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- file resolution ------------------------------------------------------


def _core_theme_path() -> Path | None:
    """Locate the canonical ``frontend/src/theme.css``.

    Source-checkout: ``<repo>/frontend/src/theme.css``.
    Packaged .app: same file ships under the bundled resources tree if
    ``build_app.sh`` stages it; otherwise this returns None and the tool
    falls back to an explicit error rather than guessing.
    """
    candidates: list[Path] = [PROJECT_ROOT / "frontend" / "src" / "theme.css"]
    try:
        import voitta  # type: ignore[import-not-found]
        res = Path(voitta.__file__).resolve().parent / "resources"
        candidates.append(res / "frontend_src" / "theme.css")
        candidates.append(res / "theme.css")
    except Exception:  # noqa: BLE001
        pass
    for p in candidates:
        if p.is_file():
            return p
    return None


def _plugin_for_host(host: str) -> dict[str, Any] | None:
    """Suffix-match the same way ``/api/plugin`` does.

    Kept inline rather than importing ``main._plugin_for_host`` because
    ``main`` imports tools; depending the other direction would create
    an import cycle.
    """
    from app.tools.providers import LOADED_PLUGINS

    hostname = host.split(":", 1)[0].lower().rstrip(".")
    for plugin in LOADED_PLUGINS:
        raw_patterns = plugin["manifest"].get("host_patterns", [])
        if isinstance(raw_patterns, str):
            raw_patterns = [raw_patterns]
        if not isinstance(raw_patterns, list):
            continue
        for raw in raw_patterns:
            if not isinstance(raw, str) or not raw:
                continue
            pat = raw.lower().rstrip(".")
            if hostname == pat or hostname.endswith("." + pat):
                return plugin
    return None


# ---- parser ---------------------------------------------------------------

# Strip /* … */ comments before the token-finder so a stray `;` inside a
# comment can't terminate a value.
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# ``--voitta-xxx: value;`` declarations. Our token values never contain
# `;` (only commas, parens, hex codes), so a non-greedy ``[^;]+`` is
# enough — no need for a paren-tracking parser.
_TOKEN_RE = re.compile(r"(--voitta-[\w-]+)\s*:\s*([^;]+);")


def _parse_tokens(css: str) -> dict[str, str]:
    """Pull every ``--voitta-xxx`` declaration out of a CSS blob."""
    stripped = _COMMENT_RE.sub("", css)
    out: dict[str, str] = {}
    for m in _TOKEN_RE.finditer(stripped):
        name = m.group(1).strip()
        value = " ".join(m.group(2).split()).strip()  # collapse whitespace
        if name and value:
            out[name] = value
    return out


def _read_file(path: Path) -> dict[str, str]:
    try:
        return _parse_tokens(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


# ---- categorisation -------------------------------------------------------


# Map prefix / exact match → category. The LLM uses these to know what's
# a colour vs. a font stack vs. a layout dimension without having to
# re-derive it from each token name.
def _categorise(tokens: dict[str, str]) -> dict[str, dict[str, str]]:
    cat: dict[str, dict[str, str]] = {
        "surfaces": {},
        "text": {},
        "accent": {},
        "header": {},
        "status": {},
        "code": {},
        "tool_chips": {},
        "bubble": {},
        "stop_button": {},
        "logs": {},
        "artifacts": {},
        "rich_blocks": {},
        "fonts": {},
        "shape": {},
        "other": {},
    }
    for name, value in tokens.items():
        short = name.removeprefix("--voitta-")
        if short in ("bg", "surface", "border", "divider"):
            cat["surfaces"][short] = value
        elif short in ("text", "text-muted", "text-faint"):
            cat["text"][short] = value
        elif short.startswith("accent"):
            cat["accent"][short] = value
        elif short.startswith("header"):
            cat["header"][short] = value
        elif short == "ok-fg" or short.startswith("error") or short.startswith("warn"):
            cat["status"][short] = value
        elif short.startswith("code") or short.startswith("link"):
            cat["code"][short] = value
        elif short.startswith("tool"):
            cat["tool_chips"][short] = value
        elif short.startswith("user-bubble"):
            cat["bubble"][short] = value
        elif short.startswith("stop"):
            cat["stop_button"][short] = value
        elif short.startswith("log"):
            cat["logs"][short] = value
        elif short.startswith("art-"):
            cat["artifacts"][short] = value
        elif short in ("rich-text-bg", "heatmap-bg"):
            cat["rich_blocks"][short] = value
        elif short.startswith("font"):
            cat["fonts"][short] = value
        elif short in ("radius", "radius-sm", "shadow", "shadow-handle", "pane-width"):
            cat["shape"][short] = value
        else:
            cat["other"][short] = value
    # Drop empty buckets — they're noise to the LLM.
    return {k: v for k, v in cat.items() if v}


def _css_block(tokens: dict[str, str]) -> str:
    """Reassemble a ``:host { … }`` block from a token dict.

    Sorted by token name for deterministic output (helps cache hashing
    in the LLM and makes diffs across calls readable).
    """
    lines = ["/* Voitta active theme — generated by get_active_theme */", ":host {"]
    for name in sorted(tokens):
        lines.append(f"  {name}: {tokens[name]};")
    lines.append("}")
    return "\n".join(lines)


def looks_dark(bg: str) -> bool:
    """Best-effort luma check for ``#rgb`` / ``#rrggbb`` / ``rgb(…)``.

    Returns False when the input is opaque-but-unparseable; the LLM
    falls back to defaults in that case, which is the safer side.
    """
    s = bg.strip().lower()
    if s.startswith("#"):
        h = s.lstrip("#")
        try:
            if len(h) == 3:
                r, g, b = (int(c * 2, 16) for c in h)
            elif len(h) >= 6:
                r = int(h[0:2], 16)
                g = int(h[2:4], 16)
                b = int(h[4:6], 16)
            else:
                return False
        except ValueError:
            return False
        return (0.299 * r + 0.587 * g + 0.114 * b) < 128
    m = re.match(r"rgba?\(\s*(\d+)[^\d]+(\d+)[^\d]+(\d+)", s)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (0.299 * r + 0.587 * g + 0.114 * b) < 128
    return False


# ---- core resolver (used by both the tool and ctx.get_theme) -------------


def resolve_theme(host: str | None) -> dict[str, Any]:
    """Return the active theme for ``host`` as a structured dict.

    Same shape the ``get_active_theme`` tool returns. Exposed as a
    plain Python function so report scripts can call it via
    ``ctx.get_theme(host=…)`` without going through the tool-call
    round trip from the LLM.

    Returns a dict with ``ok=False`` when the core theme.css can't be
    located. Callers should treat that case as "fall back to defaults".
    """
    host_str = (host or "").strip() if isinstance(host, str) else ""

    core_path = _core_theme_path()
    if core_path is None:
        return {
            "ok": False,
            "error": "core_theme_unavailable",
            "message": (
                "Could not locate frontend/src/theme.css. In a dev "
                "checkout it lives at <repo>/frontend/src/theme.css; in "
                "a packaged .app it should be staged under the bundle "
                "resources tree."
            ),
        }
    core_tokens = _read_file(core_path)

    plugin: dict[str, Any] | None = None
    plugin_overrides: dict[str, str] = {}
    if host_str:
        plugin = _plugin_for_host(host_str)
        if plugin is not None:
            plugin_theme = Path(plugin["path"]) / "theme.css"
            if plugin_theme.is_file():
                plugin_overrides = _read_file(plugin_theme)

    # Plugin tokens win over core, mirroring the runtime cascade where
    # the plugin <link> sits after the base <style> in the shadow DOM.
    merged = {**core_tokens, **plugin_overrides}

    return {
        "ok": True,
        "plugin": plugin["name"] if plugin else None,
        "host": host_str or None,
        "agent_name": (plugin["manifest"].get("agent_name") if plugin else None),
        "is_dark": looks_dark(merged.get("--voitta-bg", "#ffffff")),
        "palette": _categorise(merged),
        "raw_tokens": merged,
        "plugin_overrides": plugin_overrides,
        "css_snippet": _css_block(merged),
    }


# ---- tool handler ---------------------------------------------------------


async def _get_active_theme(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    host_arg = args.get("host")
    host = str(host_arg).strip() if isinstance(host_arg, str) else ""
    return resolve_theme(host)


registry.register(
    ToolSpec(
        name="get_active_theme",
        description=(
            "Return the active visual theme — colours, fonts, sizes — so "
            "you can skin reports, charts, and custom JS scenes to match "
            "the host page the user is on. Pass the current page's "
            "hostname (extract from the `(current url: …)` prefix on the "
            "user's message) as `host`; the tool resolves which plugin "
            "is active for that host and merges the plugin's theme.css "
            "overrides on top of the core Voitta defaults.\n"
            "\n"
            "Common use:\n"
            "  • Matplotlib: `plt.rcParams['figure.facecolor']` = "
            "palette.surfaces.bg; `axes.facecolor` = surfaces.surface; "
            "`axes.edgecolor` / `text.color` from palette.text; line "
            "colours from palette.accent.accent.\n"
            "  • Plotly: `fig.update_layout(paper_bgcolor=…, "
            "plot_bgcolor=…, font_color=…, font_family=fonts.sans)`. "
            "Set `template='plotly_dark'` when `is_dark` is true.\n"
            "  • Three.js / ctx.three_scene: pass `palette.surfaces.bg` "
            "as the `bg` parameter; use accent colours for materials.\n"
            "  • Tabulator / DataFrame panes: read raw_tokens and emit "
            "a `<style>` block via `pn.pane.HTML` for overrides.\n"
            "  • Inline pn.pane.HTML: paste `css_snippet` into a "
            "`<style>` block; tokens cascade into any nested CSS.\n"
            "\n"
            "Return shape: `{ok, plugin, host, agent_name, is_dark, "
            "palette, raw_tokens, plugin_overrides, css_snippet}`.\n"
            "  • `palette` groups tokens into categories: surfaces, "
            "text, accent, header, status, code, fonts, shape, etc. "
            "Token names have the `--voitta-` prefix stripped.\n"
            "  • `raw_tokens` is the flat `{name: value}` map with "
            "the full `--voitta-…` names.\n"
            "  • `plugin_overrides` lists ONLY the tokens the plugin "
            "changed from core — useful to know what's branded.\n"
            "  • `css_snippet` is a ready-to-paste `:host { … }` block "
            "with every token; drop it into a Three.js iframe's "
            "`<head>` or any `pn.pane.HTML(<style>…)`.\n"
            "\n"
            "Omit `host` to get the bare Voitta defaults. "
            "`is_dark` is true when the surface bg luma < 128 (helps "
            "pick `plotly_dark` vs `plotly_white`)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": (
                        "Page hostname (no scheme, no port). Extract "
                        "from the URL in your context. Examples: "
                        "'enterprise.voitta.ai', 'ebay.com', "
                        "'drive.google.com'. Omit for core defaults."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_get_active_theme,
        side="server",
    )
)
