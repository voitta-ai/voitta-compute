"""Smoke-test tool: returns the server's current time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(_args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    return {"iso": datetime.now(timezone.utc).isoformat()}


registry.register(
    ToolSpec(
        name="now",
        description="Return the server's current time as an ISO-8601 UTC string.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler,
        side="server",
    )
)
