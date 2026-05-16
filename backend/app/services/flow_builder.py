"""FlowBuilder — safe API for constructing flow-chart definitions.

A flow definition is a JSON-shaped dict::

    {"process": {
        "name": str,
        "description": str,
        "config":      {layout_engine, direction, edge_style, background,
                        show_minimap, title_block},
        "groups":      [{id, label, color}, ...],
        "steps":       [{id, type, label, tone?, icon?, badges?, meta?,
                         note?, style?, title_style?, group?}, ...],
        "connections": [{from, to, label, style, tone?}, ...],
    }}

Step types: ``trigger | activity | decision | artifact | end``.

Decision steps carry a ``conditions: [{label, target}, ...]`` list and
auto-emit a connection per branch — never call ``.connect()`` for a
decision branch yourself.

Customization paradigm: **semantic > arbitrary**. Each step accepts a
small palette of curated options (tones, icons, badges, meta rows,
notes) that always render to engineering-quality output. The
``style=`` / ``title_style=`` escape hatches accept arbitrary CSS but
are validated against a safe-list (see ``_validate_css``) — only
visual properties are accepted, never layout-breaking ones like
``position`` or ``transform``.

Validation runs lazily in ``to_dict()`` — bad references / missing
branches / unknown groups are surfaced there rather than at the call
site that introduced the problem, so the model gets a complete report
in one shot instead of fixing-one-error-at-a-time.
"""

from __future__ import annotations

from typing import Any, Iterable


class FlowBuilderError(Exception):
    """Raised by FlowBuilder on validation failure."""

    def __init__(
        self,
        message: str,
        *,
        step_id: str | None = None,
        error_type: str = "validation",
    ) -> None:
        self.step_id = step_id
        self.error_type = error_type
        super().__init__(message)


_VALID_STYLES = ("solid", "dashed")
_VALID_TYPES = ("trigger", "activity", "decision", "artifact", "end")
_VALID_TONES = ("default", "info", "success", "warning", "critical")
_VALID_LAYOUTS = ("elk", "dagre")
_VALID_DIRECTIONS = ("TB", "LR", "BT", "RL")
_VALID_EDGE_STYLES = ("smoothstep", "step", "straight", "bezier")
_VALID_BACKGROUNDS = ("dots", "lines", "cross", "none")
_VALID_DECISION_SHAPES = ("rect", "port", "diamond", "junction")
_VALID_MARKERS = ("arrow", "arrow-closed", "none")
_VALID_COLOR_MODES = ("light", "dark", "system", "auto")
_VALID_PALETTES = ("light", "dark")


# Canonical palette presets. These are the SOURCE OF TRUTH for the
# diagram-level node-body look; they ship verbatim on the wire so the
# LLM can `ctx.log(p.to_dict())` and see exactly what's being applied.
# Per-node `style=` overrides win at the CSS cascade level; plugin
# `theme.css` `:host { --voitta-flow-node-*: ... }` overrides win at
# the variable-resolution level.
PALETTE_PRESETS: dict[str, dict[str, str]] = {
    "light": {
        "node_bg":       "#f8fafc",
        "node_fg":       "#0f172a",
        "node_fg_muted": "#475569",
        "node_fg_faint": "#64748b",
        "node_border":   "#cbd5e1",
    },
    "dark": {
        "node_bg":       "#1e293b",   # slate-800
        "node_fg":       "#e2e8f0",
        "node_fg_muted": "#94a3b8",
        "node_fg_faint": "#64748b",
        "node_border":   "#334155",
    },
}


# ─── CSS safe-list ─────────────────────────────────────────────────────────
# Only purely-visual properties. No `position`, `transform`, `z-index`,
# `display`, etc. — those would let LLM-supplied styles break the
# ReactFlow layout or escape the node box. No `url(...)` values — they
# can issue network requests at render time.

_CSS_SAFE_PROPS: frozenset[str] = frozenset({
    "background", "background-color", "background-image",
    "color",
    "border", "border-color", "border-style", "border-width",
    "border-top", "border-right", "border-bottom", "border-left",
    "border-radius",
    "box-shadow",  # only if value doesn't contain url()
    "font", "font-family", "font-size", "font-weight", "font-style",
    "letter-spacing", "line-height", "text-align", "text-decoration",
    "text-transform",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "opacity",
    "min-width", "max-width", "min-height", "max-height",  # bounded; ReactFlow needs the position layer untouched
    "gap", "row-gap", "column-gap",
    "outline", "outline-color", "outline-style", "outline-width",
})


def _validate_css(value: Any, *, field_name: str) -> dict[str, str]:
    """Validate a user-supplied style dict against the safe-list.

    Returns the cleaned dict (kebab-case keys, string values). Raises
    ``FlowBuilderError`` on any disallowed property or value pattern.
    The cleaned dict is what ships to the frontend — the frontend never
    sees raw LLM input that wasn't first checked here.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FlowBuilderError(
            f"{field_name} must be a dict of CSS property → value, "
            f"got {type(value).__name__}"
        )
    out: dict[str, str] = {}
    for raw_k, raw_v in value.items():
        if not isinstance(raw_k, str):
            raise FlowBuilderError(f"{field_name}: property name must be a string")
        k = raw_k.replace("_", "-").lower().strip()
        if k not in _CSS_SAFE_PROPS:
            raise FlowBuilderError(
                f"{field_name}: property {raw_k!r} is not in the safe-list "
                f"(allowed: visual properties only — no position/transform/"
                f"display/z-index)."
            )
        if not isinstance(raw_v, (str, int, float)):
            raise FlowBuilderError(
                f"{field_name}: value for {k!r} must be a string or number, "
                f"got {type(raw_v).__name__}"
            )
        v = str(raw_v).strip()
        # Reject anything that could issue a network request or break out
        # of the property value context.
        for bad in ("url(", "expression(", "javascript:", "</", "{", "}"):
            if bad in v.lower():
                raise FlowBuilderError(
                    f"{field_name}: value for {k!r} contains disallowed "
                    f"pattern {bad!r}"
                )
        out[k] = v
    return out


# ─── Badge / meta normalisation ────────────────────────────────────────────


def _normalise_badges(badges: Any, *, step_id: str) -> list[dict[str, str]]:
    if badges is None:
        return []
    if not isinstance(badges, (list, tuple)):
        raise FlowBuilderError(
            f"step {step_id!r}: badges must be a list of "
            f"{{label, tone?}} dicts or label strings"
        )
    out: list[dict[str, str]] = []
    for b in badges:
        if isinstance(b, str):
            out.append({"label": b, "tone": "default"})
            continue
        if not isinstance(b, dict):
            raise FlowBuilderError(
                f"step {step_id!r}: badge entries must be strings or dicts"
            )
        label = b.get("label")
        if not isinstance(label, str) or not label:
            raise FlowBuilderError(
                f"step {step_id!r}: badge label must be a non-empty string"
            )
        tone = b.get("tone", "default")
        if tone not in _VALID_TONES:
            raise FlowBuilderError(
                f"step {step_id!r}: badge tone {tone!r} must be one of "
                f"{_VALID_TONES}"
            )
        out.append({"label": label, "tone": tone})
    return out


def _normalise_meta(meta: Any, *, step_id: str) -> list[dict[str, str]]:
    if meta is None:
        return []
    if not isinstance(meta, (list, tuple)):
        raise FlowBuilderError(
            f"step {step_id!r}: meta must be a list of {{key, value}} dicts "
            f"or (key, value) tuples"
        )
    out: list[dict[str, str]] = []
    for m in meta:
        if isinstance(m, (list, tuple)) and len(m) == 2:
            k, v = m
        elif isinstance(m, dict):
            k = m.get("key")
            v = m.get("value")
        else:
            raise FlowBuilderError(
                f"step {step_id!r}: meta entries must be dicts or 2-tuples"
            )
        if not isinstance(k, str) or not isinstance(v, str):
            raise FlowBuilderError(
                f"step {step_id!r}: meta key and value must both be strings"
            )
        out.append({"key": k, "value": v})
    return out


def _normalise_icon(icon: Any, *, step_id: str) -> dict[str, str] | None:
    """Accept either ``None``, a lucide icon name (``"play"``), or an
    inline SVG escape hatch (``{"svg": "<svg …/>"}``).

    Returns a normalised dict ``{"name": ...}`` or ``{"svg": ...}``,
    or ``None``. The frontend resolves lucide names against
    ``lucide-preact``; the SVG form is rendered verbatim (sanitised by
    DOMPurify on the client).
    """
    if icon is None:
        return None
    if isinstance(icon, str):
        if not icon:
            return None
        return {"name": icon}
    if isinstance(icon, dict):
        if "name" in icon and isinstance(icon["name"], str):
            return {"name": icon["name"]}
        if "svg" in icon and isinstance(icon["svg"], str):
            # Length-cap the inline SVG so a runaway LLM doesn't ship
            # 1 MB of markup. 4 KB is plenty for a 16x16 / 24x24 icon.
            svg = icon["svg"]
            if len(svg) > 4096:
                raise FlowBuilderError(
                    f"step {step_id!r}: inline SVG icon exceeds 4096 bytes"
                )
            return {"svg": svg}
        raise FlowBuilderError(
            f"step {step_id!r}: icon dict must have 'name' or 'svg' key"
        )
    raise FlowBuilderError(
        f"step {step_id!r}: icon must be a string, dict, or None"
    )


# ─── FlowBuilder ───────────────────────────────────────────────────────────


class FlowBuilder:
    """Construct a flow definition.

    Usage::

        p = FlowBuilder("Approval", "Five-step approval flow")
        p.layout(direction="LR", engine="elk")
        p.trigger("start", "Submit Request",
                  tone="info", icon="play",
                  badges=["Requestor"])
        p.activity("review", "Review Request",
                   tone="default", icon="search",
                   badges=[{"label": "Manager", "tone": "info"}],
                   meta=[("Input", "Request Form")])
        p.decision("decision", "Approved?",
                   tone="warning", icon="git-branch",
                   branches=[("Yes", "notify"), ("No", "reject")])
        p.activity("notify", "Send Approval", tone="success", icon="mail")
        p.end("reject", "Request Denied", tone="critical", icon="x")
        p.connect("start", "review")
        p.connect("review", "decision")
        return p
    """

    def __init__(self, name: str, description: str = "") -> None:
        self._name = name
        self._description = description
        self._groups: dict[str, dict] = {}
        self._steps: dict[str, dict] = {}
        self._connections: list[dict] = []
        self._connection_set: set[tuple[str, str]] = set()
        # Diagram-level config — applied by the frontend ReactFlow
        # renderer. All optional; engineering defaults apply when unset.
        self._config: dict[str, Any] = {}

    # ── Diagram-level configuration ───────────────────────────────────

    def layout(
        self,
        *,
        direction: str = "TB",
        engine: str = "elk",
    ) -> "FlowBuilder":
        """Configure the auto-layout pass.

        ``direction`` is one of ``TB`` (top-bottom, default), ``LR``
        (left-right), ``BT``, ``RL``.

        ``engine`` is ``elk`` (default — Sugiyama-style layered routing,
        higher quality, ~250 KB JS) or ``dagre`` (~80 KB, faster, less
        clever about edge routing). Engineering diagrams typically want
        ``elk`` for better orthogonal edge crossings.
        """
        if direction not in _VALID_DIRECTIONS:
            raise FlowBuilderError(
                f"layout direction must be one of {_VALID_DIRECTIONS}, "
                f"got {direction!r}"
            )
        if engine not in _VALID_LAYOUTS:
            raise FlowBuilderError(
                f"layout engine must be one of {_VALID_LAYOUTS}, got {engine!r}"
            )
        self._config["direction"] = direction
        self._config["layout_engine"] = engine
        return self

    def edge_style(self, style: str) -> "FlowBuilder":
        """Default edge type — ``smoothstep`` (orthogonal w/ rounded
        corners — engineering default), ``step`` (sharp orthogonal),
        ``straight``, or ``bezier``. Per-connection style overrides win.
        """
        if style not in _VALID_EDGE_STYLES:
            raise FlowBuilderError(
                f"edge_style must be one of {_VALID_EDGE_STYLES}, got {style!r}"
            )
        self._config["edge_style"] = style
        return self

    def edge_options(
        self,
        *,
        border_radius: int | None = None,
        offset: int | None = None,
        step_position: float | None = None,
    ) -> "FlowBuilder":
        """Fine-tune smoothstep / step edge routing.

          • ``border_radius`` — corner softness on smoothstep edges
            (px). Larger = rounder corners. Default: ReactFlow's own
            (~5).
          • ``offset`` — distance from source/target before the edge
            takes its first turn. Useful for keeping edges from
            crowding directly into the node body. Default: 20.
          • ``step_position`` — 0.0..1.0; where along the trunk the
            orthogonal bend happens. 0.5 = midpoint (default). Lower
            = closer to source, higher = closer to target. Great for
            fan-out trunks.
        """
        opts: dict[str, Any] = {}
        if border_radius is not None:
            if not isinstance(border_radius, int) or border_radius < 0:
                raise FlowBuilderError("border_radius must be a non-negative int")
            opts["border_radius"] = border_radius
        if offset is not None:
            if not isinstance(offset, int) or offset < 0:
                raise FlowBuilderError("offset must be a non-negative int")
            opts["offset"] = offset
        if step_position is not None:
            if not isinstance(step_position, (int, float)) or not (0.0 <= step_position <= 1.0):
                raise FlowBuilderError("step_position must be between 0.0 and 1.0")
            opts["step_position"] = float(step_position)
        if opts:
            self._config["edge_options"] = {**self._config.get("edge_options", {}), **opts}
        return self

    def color_mode(self, mode: str = "auto") -> "FlowBuilder":
        """Control ReactFlow's built-in dark/light scheme.

          • ``"auto"`` (default if you call this) — derive from the
            active host theme (``ctx.get_theme().is_dark``).
          • ``"light"`` / ``"dark"`` — explicit override.
          • ``"system"`` — track the OS preference at runtime via
            ``prefers-color-scheme``.

        Drives ReactFlow's ``colorMode`` prop, which toggles a ``dark``
        class on the wrapper and swaps an internal CSS-variable bundle
        (``--xy-edge-stroke``, ``--xy-node-background``, etc.) for
        better contrast.

        NB: ``color_mode`` controls ReactFlow's internal chrome (Controls
        panel, attribution). For NODE BODY colours, use ``p.palette(...)``
        (or per-node ``style=`` overrides). The two are independent.
        """
        if mode not in _VALID_COLOR_MODES:
            raise FlowBuilderError(
                f"color_mode must be one of {_VALID_COLOR_MODES}, got {mode!r}"
            )
        self._config["color_mode"] = mode
        return self

    def palette(self, name: str = "light") -> "FlowBuilder":
        """Pick the node-body palette for this diagram.

        Two presets — ``"light"`` (default) and ``"dark"``. The selected
        preset ships verbatim on the wire under ``config.palette`` and
        is applied as inline CSS variables on the ReactFlow wrapper
        (no class-name automation, no specificity wars).

        Override hierarchy:

          1. Per-node ``style={...}`` wins (CSS cascade).
          2. Diagram-level ``p.palette(...)`` wins over plugin theme.
          3. Plugin ``theme.css`` ``:host { --voitta-flow-node-*: ... }``
             wins over the bare defaults.

        Resolved palette dict (5 keys — ``node_bg`` / ``node_fg`` /
        ``node_fg_muted`` / ``node_fg_faint`` / ``node_border``)::

            ctx.log(p.to_dict()["process"]["config"]["palette"])

        Tones (info / success / warning / critical / default) are
        INDEPENDENT — they style the title bar and outgoing edges and
        are not affected by ``palette``.
        """
        if name not in _VALID_PALETTES:
            raise FlowBuilderError(
                f"palette must be one of {_VALID_PALETTES}, got {name!r}"
            )
        self._config["palette"] = dict(PALETTE_PRESETS[name])
        self._config["palette_name"] = name
        return self

    def background(self, kind: str) -> "FlowBuilder":
        """Canvas background — ``dots`` (engineering default),
        ``lines``, ``cross``, or ``none``.
        """
        if kind not in _VALID_BACKGROUNDS:
            raise FlowBuilderError(
                f"background must be one of {_VALID_BACKGROUNDS}, got {kind!r}"
            )
        self._config["background"] = kind
        return self

    def show_minimap(self, on: bool = True) -> "FlowBuilder":
        """Show the corner minimap (off by default)."""
        self._config["show_minimap"] = bool(on)
        return self

    def title_block(
        self,
        *,
        drawing_id: str | None = None,
        rev: str | None = None,
        author: str | None = None,
        date: str | None = None,
    ) -> "FlowBuilder":
        """Engineering-drawing-style title block in the corner of the
        canvas. Any field omitted is skipped.
        """
        block: dict[str, str] = {}
        for k, v in (("drawing_id", drawing_id), ("rev", rev),
                     ("author", author), ("date", date)):
            if v is not None:
                if not isinstance(v, str):
                    raise FlowBuilderError(f"title_block.{k} must be a string")
                block[k] = v
        if block:
            self._config["title_block"] = block
        return self

    # ── Groups ────────────────────────────────────────────────────────

    def group(
        self, id: str, label: str, color: str = "var(--voitta-surface)"
    ) -> "FlowBuilder":
        if id in self._groups:
            raise FlowBuilderError(
                f"Duplicate group ID: {id!r}", step_id=id, error_type="duplicate"
            )
        self._groups[id] = {"id": id, "label": label, "color": color}
        return self

    # ── Step registration helper ──────────────────────────────────────

    def _add_step(self, step: dict) -> None:
        step_id = step["id"]
        if step_id in self._steps:
            raise FlowBuilderError(
                f"Duplicate step ID: {step_id!r}",
                step_id=step_id,
                error_type="duplicate",
            )
        grp = step.get("group")
        if grp and grp not in self._groups:
            raise FlowBuilderError(
                f"Step {step_id!r} references undefined group {grp!r}. "
                f"Call p.group({grp!r}, …) before this step.",
                step_id=step_id,
                error_type="reference",
            )
        if step["type"] not in _VALID_TYPES:
            raise FlowBuilderError(
                f"Step {step_id!r}: unknown type {step['type']!r} "
                f"(valid: {', '.join(_VALID_TYPES)})",
                step_id=step_id,
            )
        if step.get("tone") and step["tone"] not in _VALID_TONES:
            raise FlowBuilderError(
                f"Step {step_id!r}: tone {step['tone']!r} must be one of "
                f"{_VALID_TONES}",
                step_id=step_id,
            )
        self._steps[step_id] = step

    def _common_step_fields(
        self,
        *,
        step_id: str,
        tone: str | None,
        icon: Any,
        badges: Any,
        meta: Any,
        note: str | None,
        style: Any,
        title_style: Any,
        group: str | None,
    ) -> dict[str, Any]:
        """Build the customization-field portion of a step dict — every
        step type funnels through here so the surface stays consistent.
        """
        out: dict[str, Any] = {}
        if tone is not None:
            out["tone"] = tone
        nic = _normalise_icon(icon, step_id=step_id)
        if nic is not None:
            out["icon"] = nic
        nb = _normalise_badges(badges, step_id=step_id)
        if nb:
            out["badges"] = nb
        nm = _normalise_meta(meta, step_id=step_id)
        if nm:
            out["meta"] = nm
        if note is not None:
            if not isinstance(note, str):
                raise FlowBuilderError(
                    f"step {step_id!r}: note must be a string or None"
                )
            if note:
                out["note"] = note
        ns = _validate_css(style, field_name=f"step {step_id!r} style")
        if ns:
            out["style"] = ns
        nts = _validate_css(
            title_style, field_name=f"step {step_id!r} title_style"
        )
        if nts:
            out["title_style"] = nts
        if group:
            out["group"] = group
        return out

    # ── Steps ─────────────────────────────────────────────────────────

    def trigger(
        self,
        id: str,
        label: str,
        *,
        group: str | None = None,
        description: str = "",
        roles: list[str] | None = None,
        artifacts_out: list[str] | None = None,
        # Visual customization
        tone: str | None = None,
        icon: Any = None,
        badges: Any = None,
        meta: Any = None,
        note: str | None = None,
        style: Any = None,
        title_style: Any = None,
    ) -> "FlowBuilder":
        step: dict = {
            "id": id,
            "label": label,
            "type": "trigger",
            "description": description,
            "roles": roles or [],
            "artifacts_out": artifacts_out or [],
            **self._common_step_fields(
                step_id=id, tone=tone, icon=icon, badges=badges, meta=meta,
                note=note, style=style, title_style=title_style, group=group,
            ),
        }
        self._add_step(step)
        return self

    def activity(
        self,
        id: str,
        label: str,
        *,
        group: str | None = None,
        description: str = "",
        roles: list[str] | None = None,
        artifacts_in: list[str] | None = None,
        artifacts_out: list[str] | None = None,
        tone: str | None = None,
        icon: Any = None,
        badges: Any = None,
        meta: Any = None,
        note: str | None = None,
        style: Any = None,
        title_style: Any = None,
    ) -> "FlowBuilder":
        step: dict = {
            "id": id,
            "label": label,
            "type": "activity",
            "description": description,
            "roles": roles or [],
            "artifacts_in": artifacts_in or [],
            "artifacts_out": artifacts_out or [],
            **self._common_step_fields(
                step_id=id, tone=tone, icon=icon, badges=badges, meta=meta,
                note=note, style=style, title_style=title_style, group=group,
            ),
        }
        self._add_step(step)
        return self

    def decision(
        self,
        id: str,
        label: str,
        *,
        shape: str = "rect",
        group: str | None = None,
        description: str = "",
        roles: list[str] | None = None,
        artifacts_in: list[str] | None = None,
        branches: list[tuple[str, str]] | None = None,
        tone: str | None = None,
        icon: Any = None,
        badges: Any = None,
        meta: Any = None,
        note: str | None = None,
        style: Any = None,
        title_style: Any = None,
    ) -> "FlowBuilder":
        """Decision (branching) step. ``branches`` is a list of
        ``(label, target_step_id)`` tuples, ≥ 2.

        ``shape`` chooses the visual style of the decision node:

          • ``"rect"`` (default) — rectangular like an activity, with
            a ``DECISION`` chip and ◆ marker. Edge labels carry the
            branch names. Good for 2–3 branches with short labels.

          • ``"port"`` — schematic-style multi-port. Each branch is a
            named output handle ON the node body (right side); edges
            leave that port unlabeled. Reads like an IC datasheet
            pinout. Best for 4+ branches or branches with descriptive
            labels.

          • ``"diamond"`` — classic BPMN rotated rhombus. Edge labels
            carry the branch names. Use when you want the *shape* to
            signal "this is a question" — typically for yes/no
            decisions.

          • ``"junction"`` — tiny labeled node, branch labels live on
            edges. Use when many branches are routing and the labels
            ARE the content. Drops the title bar and body; no
            ``roles=`` / ``meta=`` / ``note=`` allowed.

        For ``"port"`` shape, each auto-emitted branch connection
        carries a ``source_handle`` field (``"port-0"``, ``"port-1"``,
        …) so the frontend can attach the edge to the right port.
        """
        if shape not in _VALID_DECISION_SHAPES:
            raise FlowBuilderError(
                f"Decision {id!r}: shape must be one of "
                f"{_VALID_DECISION_SHAPES}, got {shape!r}",
                step_id=id,
            )
        if branches is None or len(branches) < 2:
            raise FlowBuilderError(
                f"Decision {id!r} must have at least 2 branches, "
                f"got {0 if branches is None else len(branches)}",
                step_id=id,
            )
        if shape == "junction" and (
            roles or (meta and len(_normalise_meta(meta, step_id=id)) > 0)
            or note
        ):
            raise FlowBuilderError(
                f"Decision {id!r}: shape='junction' is a tiny routing "
                f"node and cannot carry roles, meta, or note. Use "
                f"shape='rect' or 'port' instead.",
                step_id=id,
            )

        conditions = [{"label": str(lbl), "target": tgt} for lbl, tgt in branches]
        step: dict = {
            "id": id,
            "label": label,
            "type": "decision",
            "shape": shape,
            "description": description,
            "roles": roles or [],
            "artifacts_in": artifacts_in or [],
            "conditions": conditions,
            **self._common_step_fields(
                step_id=id, tone=tone, icon=icon, badges=badges, meta=meta,
                note=note, style=style, title_style=title_style, group=group,
            ),
        }
        self._add_step(step)
        for i, (lbl, tgt) in enumerate(branches):
            # rect/diamond: alternate solid/dashed for visual variety.
            # port/junction: keep them all solid — visual differentiation
            # is in the node itself (port) or the label (junction), and
            # mixed solid/dashed reads as noise rather than meaning.
            if shape in ("port", "junction"):
                style_str = "solid"
            else:
                style_str = "solid" if i == 0 else "dashed"
            key = (id, tgt)
            if key in self._connection_set:
                continue
            conn: dict[str, Any] = {
                "from": id,
                "to": tgt,
                "label": str(lbl),
                "style": style_str,
                "marker": "arrow-closed",
            }
            if shape == "port":
                # Port-shape decisions need per-branch handle IDs so
                # the frontend can attach each edge to its own port.
                # Index-based IDs keep this stable across renames.
                conn["source_handle"] = f"port-{i}"
                # No edge label — the label is shown ON the port row.
                conn["label"] = ""
            self._connections.append(conn)
            self._connection_set.add(key)
        return self

    def artifact(
        self,
        id: str,
        label: str,
        *,
        group: str | None = None,
        description: str = "",
        roles: list[str] | None = None,
        artifacts_in: list[str] | None = None,
        artifacts_out: list[str] | None = None,
        tone: str | None = None,
        icon: Any = None,
        badges: Any = None,
        meta: Any = None,
        note: str | None = None,
        style: Any = None,
        title_style: Any = None,
    ) -> "FlowBuilder":
        step: dict = {
            "id": id,
            "label": label,
            "type": "artifact",
            "description": description,
            "roles": roles or [],
            "artifacts_in": artifacts_in or [],
            "artifacts_out": artifacts_out or [],
            **self._common_step_fields(
                step_id=id, tone=tone, icon=icon, badges=badges, meta=meta,
                note=note, style=style, title_style=title_style, group=group,
            ),
        }
        self._add_step(step)
        return self

    def end(
        self,
        id: str,
        label: str = "End",
        *,
        group: str | None = None,
        description: str = "",
        artifacts_in: list[str] | None = None,
        tone: str | None = None,
        icon: Any = None,
        badges: Any = None,
        meta: Any = None,
        note: str | None = None,
        style: Any = None,
        title_style: Any = None,
    ) -> "FlowBuilder":
        step: dict = {
            "id": id,
            "label": label,
            "type": "end",
            "description": description,
            "artifacts_in": artifacts_in or [],
            **self._common_step_fields(
                step_id=id, tone=tone, icon=icon, badges=badges, meta=meta,
                note=note, style=style, title_style=title_style, group=group,
            ),
        }
        self._add_step(step)
        return self

    # ── Connections ───────────────────────────────────────────────────

    def connect(
        self,
        from_id: str,
        to_id: str,
        *,
        label: str = "",
        style: str = "solid",
        tone: str | None = None,
        marker: str = "arrow-closed",
        animated: bool = False,
        border_radius: int | None = None,
    ) -> "FlowBuilder":
        """Connect two steps.

        ``tone`` — edge stroke colour, same palette as nodes
          (default | info | success | warning | critical).
        ``style`` — ``solid`` or ``dashed``. Static visual.
        ``marker`` — arrowhead style: ``arrow-closed`` (default, filled
          triangle), ``arrow`` (open V), or ``none``.
        ``animated`` — when True, the edge gets a marching-ants
          animation (ReactFlow's built-in `animated` flag). Use to
          highlight a flowing / active path.
        ``border_radius`` — per-edge override of smoothstep corner
          softness. Falls back to ``edge_options(border_radius=…)``.

        For decision branches, use ``.decision(branches=…)`` — those
        auto-emit connections.
        """
        if from_id not in self._steps:
            raise FlowBuilderError(
                f"connect(): source {from_id!r} is not a defined step",
                step_id=from_id, error_type="reference",
            )
        if to_id not in self._steps:
            raise FlowBuilderError(
                f"connect(): target {to_id!r} is not a defined step",
                step_id=to_id, error_type="reference",
            )
        if style not in _VALID_STYLES:
            raise FlowBuilderError(
                f"connect(): style must be one of {_VALID_STYLES}, got {style!r}"
            )
        if tone is not None and tone not in _VALID_TONES:
            raise FlowBuilderError(
                f"connect(): tone must be one of {_VALID_TONES}, got {tone!r}"
            )
        if marker not in _VALID_MARKERS:
            raise FlowBuilderError(
                f"connect(): marker must be one of {_VALID_MARKERS}, got {marker!r}"
            )
        if border_radius is not None and (
            not isinstance(border_radius, int) or border_radius < 0
        ):
            raise FlowBuilderError("connect(): border_radius must be a non-negative int")
        key = (from_id, to_id)
        if key in self._connection_set:
            return self
        conn: dict[str, Any] = {
            "from": from_id, "to": to_id, "label": label, "style": style,
            "marker": marker,
        }
        if tone is not None:
            conn["tone"] = tone
        if animated:
            conn["animated"] = True
        if border_radius is not None:
            conn["border_radius"] = border_radius
        self._connections.append(conn)
        self._connection_set.add(key)
        return self

    # ── Build / serialise ─────────────────────────────────────────────

    def _validate_final(self) -> None:
        errors: list[str] = []
        if not self._steps:
            errors.append("Flow must have at least one step")
        step_ids = set(self._steps)
        for conn in self._connections:
            if conn["from"] not in step_ids:
                errors.append(f"Connection from unknown step: {conn['from']!r}")
            if conn["to"] not in step_ids:
                errors.append(f"Connection to unknown step: {conn['to']!r}")
        for sid, step in self._steps.items():
            if step["type"] == "decision":
                for cond in step.get("conditions", []):
                    if cond["target"] not in step_ids:
                        errors.append(
                            f"Decision {sid!r} branch {cond['label']!r} "
                            f"targets unknown step: {cond['target']!r}"
                        )
        if errors:
            raise FlowBuilderError(
                "Flow validation failed:\n  - " + "\n  - ".join(errors)
            )

    def to_dict(self) -> dict[str, Any]:
        self._validate_final()
        return {
            "process": {
                "name": self._name,
                "description": self._description,
                "config": dict(self._config),
                "groups": list(self._groups.values()),
                "steps": list(self._steps.values()),
                "connections": list(self._connections),
            }
        }
