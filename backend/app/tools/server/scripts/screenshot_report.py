"""``screenshot_report(name?)`` — capture the current report pane as PNG.

Hybrid wrapper that round-trips to the FE's ``screenshot_report``
primitive. The primitive postMessages html2canvas inside the report
iframe (every report is HTML). It returns a base64 image which we pass
back to the model via the ``_image`` sentinel.

The optional ``name`` argument is purely informational — the FE
captures whatever's currently mounted in the pane.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> Any:
    return await call_browser("screenshot_report", args, ctx)


registry.register(
    ToolSpec(
        name="screenshot_report",
        description=(
            "Capture the currently-mounted HTML report iframe as a "
            "single screenshot using `html-to-image`. The whole "
            "iframe is rasterised (full scrollHeight, not just the "
            "visible viewport). Wall time ~1.5s.\n"
            "\n"
            "What you see:\n"
            "  • In the chat — the FULL-SIZE original PNG as an "
            "inline image (separate message, not in the tool step's "
            "collapsed area), preserved at native resolution.\n"
            "  • In your tool result — a DOWNSIZED webp version (max "
            "1280 × 2400 px, quality 75, ~100-300 KB) inlined as an "
            "image block so you can see the layout without burning "
            "context. Anthropic only.\n"
            "\n"
            "If your report animates, embeds three.js, or fetches "
            "data, read `screenshot-friendly.md` — screenshot is "
            "the LLM's only feedback loop on what was rendered.\n"
            "\n"
            "Switch to Anthropic in Settings; other providers can't "
            "accept inline images in tool results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "expand_height": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 20000,
                    "description": (
                        "Force the iframe to this pixel height before "
                        "capture, bypassing the auto-size two-phase "
                        "probe. Use when the report is stretch-fill and "
                        "auto-size truncates."
                    ),
                },
                "expand_width": {
                    "type": "integer",
                    "minimum": 600,
                    "maximum": 4000,
                    "description": (
                        "Force the iframe to this pixel width before "
                        "capture. Default 1920 (desktop layout). Raise "
                        "if the report's natural breakpoint for multi-"
                        "column layout is higher; lower to force a "
                        "mobile/stacked capture."
                    ),
                },
                "scale": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 3.0,
                    "description": (
                        "Rasterisation scale. 1.0 = CSS pixels; 2.0 = "
                        "double-resolution (~4× bytes). Stay at 1.0 "
                        "unless detail matters."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["webp", "png"],
                    "description": (
                        "Output format. webp = lossy ~5× smaller; png = "
                        "lossless. Default webp is right for most cases."
                    ),
                },
                "quality": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1.0,
                    "description": "WebP quality 0.1–1.0 (ignored for png).",
                },
            },
            "additionalProperties": False,
        },
        handler=_handler,
        side="hybrid",
    )
)
