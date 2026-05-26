"""``list_scripts()`` — list all saved scripts.

Returns minimal metadata. Source is not included (use ``get_script``).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.reports import store
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(_args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    metas = store.list_scripts()
    return {
        "ok": True,
        "count": len(metas),
        "scripts": [asdict(m) for m in metas],
    }


registry.register(
    ToolSpec(
        name="list_scripts",
        description="List every saved script with its metadata.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side="server",
        handler=_handler,
    )
)
