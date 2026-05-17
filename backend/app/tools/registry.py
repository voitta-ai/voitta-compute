"""Registry of LLM-facing tools.

A ``ToolSpec`` describes one tool: name, description, JSON-schema input
shape, and an async handler. The handler receives parsed args and a
``ToolCtx`` carrying request-scoped state (most importantly the bridge
``session_id``, used by hybrid tools).

The registry exposes:

* ``register(spec)`` — add a tool.
* ``to_anthropic_tools()`` — produce the canonical tool list for any
  provider (Anthropic shape; the OpenAI / Gemini adapters convert from
  this in `app.services.llm.*`).
* ``dispatch(name, args, ctx)`` — invoke a tool, returning a
  ``ToolResult`` envelope (never raises; exceptions are captured into
  ``error``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


logger = logging.getLogger(__name__)


@dataclass
class ToolCtx:
    """Request-scoped state passed to tool handlers.

    ``extras`` is a free-form per-request payload plugins can read /
    write without core needing to add a field. Keys are
    plugin-namespaced by convention (e.g. ``"voitta_google.tenant"``).
    """

    session_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    latency_ms: int = 0


ToolHandler = Callable[[dict[str, Any], ToolCtx], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    side: Literal["server", "hybrid"] = "server"
    # Optional gate: if set, the tool is only included in the LLM's tool
    # list when the bookmarklet session's `page.host` matches one of
    # these patterns via strict suffix match. ``None`` means no host
    # gating (visible everywhere). A bare string is normalised to a
    # one-element list at match time, so existing callers keep working.
    host_pattern: "str | list[str] | None" = None
    # Optional runtime gate: if set, the tool is only included when this
    # callable returns True. Used for tools that depend on external state
    # the LLM can't manipulate (e.g. "Drive tools only visible when the
    # user has connected OAuth"). Called at every chat turn — keep cheap.
    # None = no runtime gating.
    visibility_check: Callable[[], bool] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        # Collision policy: log + skip rather than raise. Raising aborts
        # the entire plugin's import, so one duplicate tool name would
        # drop every sibling tool the plugin also wanted to contribute.
        # Skip-and-keep preserves the rest of the plugin's surface.
        prior = self._tools.get(spec.name)
        if prior is not None:
            logger.warning(
                "tool %r already registered by %s.%s — keeping prior, "
                "skipping new spec from %s.%s",
                spec.name,
                getattr(prior.handler, "__module__", "?"),
                getattr(prior.handler, "__qualname__", "?"),
                getattr(spec.handler, "__module__", "?"),
                getattr(spec.handler, "__qualname__", "?"),
            )
            return
        self._tools[spec.name] = spec

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def visible_for_host(self, host: str | None) -> list[ToolSpec]:
        """Return tools whose ``host_pattern`` matches ``host`` (or has none).

        Used by the chat route to hide host-specific tools (e.g.
        ``drive_*``) from the LLM when the user's bookmarklet isn't
        running on the matching host. Avoids confusing the model with
        tools it can't actually use, and avoids leaking the tool's
        existence on unrelated pages.

        ``host`` is the value of ``page.host`` posted by the browser at
        ``/tools/register`` (e.g. ``drive.google.com``). ``None`` means
        the bridge hasn't reported a host yet — in that case we skip
        host-gated tools entirely.

        Match rule for ``host_pattern``: STRICT suffix match against the
        host's hostname. ``host_pattern="drive.google.com"`` matches
        ``drive.google.com`` and ``foo.drive.google.com`` but NOT
        ``drive.google.com.evil.com`` (which a substring match would
        accept). A list of patterns is OR'd together — the tool shows
        when ANY pattern matches. The ``host`` may include a port
        (``host:port``) — we strip it before comparison.
        """
        out: list[ToolSpec] = []
        # Strip port for comparison.
        hostname = (host or "").split(":", 1)[0].lower().rstrip(".")
        for s in self._tools.values():
            # Host gate.
            if s.host_pattern is not None:
                if not hostname:
                    continue
                patterns = (
                    [s.host_pattern]
                    if isinstance(s.host_pattern, str)
                    else list(s.host_pattern)
                )
                matched = False
                for raw in patterns:
                    if not isinstance(raw, str) or not raw:
                        continue
                    pat = raw.lower().rstrip(".")
                    if hostname == pat or hostname.endswith("." + pat):
                        matched = True
                        break
                if not matched:
                    continue
            # Runtime visibility gate (e.g. "OAuth connected").
            if s.visibility_check is not None:
                try:
                    if not s.visibility_check():
                        continue
                except Exception:
                    continue
            out.append(s)
        return out

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in self._tools.values()
        ]

    async def dispatch(
        self, name: str, args: dict[str, Any], ctx: ToolCtx
    ) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                error={"kind": "unknown_tool", "message": f"no tool named {name!r}"},
            )
        started = time.monotonic()

        # Activity registry — drives the menu bar's color-coded icon.
        # Begin/end is bracketed around the whole handler, so concurrent
        # tools all show their state simultaneously (the priority logic
        # in app.activity picks the most-significant one for display).
        from app import activity
        activity_token = activity.begin(activity.classify(name), detail=name)
        try:
            value = await spec.handler(args or {}, ctx)
        except Exception as exc:
            logger.exception("tool %s raised", name)
            return ToolResult(
                ok=False,
                error={"kind": type(exc).__name__, "message": str(exc)},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            activity.end(activity_token)
        return ToolResult(
            ok=True,
            result=value,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


registry = ToolRegistry()
