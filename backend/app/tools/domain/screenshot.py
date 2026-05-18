"""Screenshot the currently-open HoloViz report and feed it to the LLM.

Hybrid tool. Calls the ``screenshot_report`` browser primitive, which
runs html2canvas INSIDE the report iframe (same-origin to our backend
so it can read all the Bokeh canvases) and returns a base64 dataURL
covering the entire ``document.documentElement`` — not just the visible
viewport. The LLM gets the image directly in the next-turn conversation
context (Anthropic only; OpenAI/Gemini fall back to a text note).

Optional cropping: pass a ``crop`` rectangle in percent-of-image
coordinates ({x, y, width, height} all in [0, 100]) to focus on a sub-
region. Cropping happens server-side via Pillow after the full-page
rasterisation, so the LLM still sees the whole layout if it doesn't
specify a crop.

Output is WebP at quality 80 by default — typical reports come out at
100-300 KB, well under any provider's image limit.

Known limit: WebGL canvases (rare; Bokeh defaults to 2D) without
``preserveDrawingBuffer: true`` rasterise as blank rectangles, since
the bitmap is cleared after compositing. Nothing we can do about that
without re-rendering server-side.
"""

from __future__ import annotations

import base64
import binascii
import io
import time
from typing import Any

from app.services import python_storage
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# WebP quality target. 80 keeps text crisp while compressing photographic
# regions hard. PNG is offered as a format option for the rare case the
# LLM wants pixel-perfect detail (lossless), but defaulting to WebP keeps
# the tool result well within token budgets.
_DEFAULT_QUALITY = 80
_DEFAULT_FORMAT = "webp"

# How long we let html2canvas run before giving up. Complex reports with
# many Bokeh plots can take a few seconds; 60 s is a generous ceiling.
_DEFAULT_TIMEOUT_MS = 60_000


async def _screenshot_report(args: dict[str, Any], ctx: ToolCtx) -> Any:
    crop = args.get("crop") or None
    fmt = (args.get("format") or _DEFAULT_FORMAT).lower()
    if fmt not in ("webp", "png"):
        return {
            "ok": False,
            "error": "invalid_format",
            "message": "format must be 'webp' or 'png'",
        }
    quality = int(args.get("quality") or _DEFAULT_QUALITY)
    quality = max(1, min(100, quality))
    scale = float(args.get("scale") or 1.0)
    scale = max(0.5, min(3.0, scale))
    timeout_ms = int(args.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)

    started = time.time()

    # Fire the browser primitive. Anything other than a clean dataURL
    # comes back as a BrowserToolError we map onto a JSON envelope.
    try:
        primitive_result = await call_browser(
            "screenshot_report",
            {
                "scale": scale,
                # The iframe's html2canvas always rasters to its own
                # native quality target; we re-encode server-side anyway
                # so we ask for PNG from the iframe (lossless input)
                # and let Pillow do the WebP/PNG output. This avoids a
                # double-lossy round trip when the user asks for WebP.
                "format": "png",
                "quality": 1.0,
            },
            ctx,
            timeout_ms=timeout_ms + 5_000,
        )
    except BrowserToolError as exc:
        if exc.kind == "no_report":
            return {
                "ok": False,
                "error": "no_report_open",
                "message": (
                    "no report is currently open. Call "
                    "show_holoviz_report(report_id=…) first to render "
                    "the report into the iframe pane, then retry."
                ),
            }
        if exc.kind == "edit_mode":
            return {
                "ok": False,
                "error": "edit_mode",
                "message": (
                    "the report is currently in edit mode, where "
                    "screenshots cannot be taken. Ask the user to "
                    "leave edit mode (toggle the edit affordance off "
                    "in the report header) and then retry the "
                    "screenshot."
                ),
            }
        return {
            "ok": False,
            "error": exc.kind,
            "message": str(exc),
        }

    if not isinstance(primitive_result, dict) or not primitive_result.get("ok"):
        return {
            "ok": False,
            "error": "screenshot_failed",
            "message": (
                primitive_result.get("message")
                if isinstance(primitive_result, dict)
                else "primitive returned non-dict"
            ),
        }

    data_url = primitive_result.get("data_url") or ""
    if not data_url.startswith("data:image/"):
        return {
            "ok": False,
            "error": "bad_dataurl",
            "message": f"primitive returned data_url shaped {data_url[:60]!r}…",
        }
    try:
        _, b64 = data_url.split(",", 1)
        raw_bytes = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        return {
            "ok": False,
            "error": "decode_failed",
            "message": str(exc),
        }

    # Optional crop. Percentages → pixel rect via Pillow.
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover — Pillow is in deps
        return {
            "ok": False,
            "error": "pillow_missing",
            "message": str(exc),
        }

    img = Image.open(io.BytesIO(raw_bytes))
    img.load()
    if crop:
        try:
            x = float(crop.get("x", 0))
            y = float(crop.get("y", 0))
            w = float(crop.get("width", 100))
            h = float(crop.get("height", 100))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "invalid_crop",
                "message": (
                    "crop must be {x, y, width, height} all numeric "
                    "and in [0, 100]"
                ),
            }
        if not all(0 <= v <= 100 for v in (x, y, w, h)):
            return {
                "ok": False,
                "error": "invalid_crop",
                "message": "crop values must be in [0, 100]",
            }
        if w <= 0 or h <= 0:
            return {
                "ok": False,
                "error": "invalid_crop",
                "message": "crop width and height must be > 0",
            }
        if x + w > 100 or y + h > 100:
            return {
                "ok": False,
                "error": "invalid_crop",
                "message": (
                    "crop x+width must be ≤ 100 and y+height must be ≤ 100"
                ),
            }
        W, H = img.size
        left = int(round(W * x / 100))
        upper = int(round(H * y / 100))
        right = int(round(W * (x + w) / 100))
        lower = int(round(H * (y + h) / 100))
        img = img.crop((left, upper, right, lower))

    out_buf = io.BytesIO()
    if fmt == "webp":
        img.convert("RGB").save(out_buf, format="WEBP", quality=quality, method=6)
    else:
        img.save(out_buf, format="PNG", optimize=True)
    out_bytes = out_buf.getvalue()

    # Persist for later compute scripts / re-display.
    report_info = primitive_result.get("report") or {}
    report_id = report_info.get("report_id") or "unknown"
    name = f"report_{report_id}_{int(started)}.{fmt}"
    import tempfile, os
    fd, tmp_path = tempfile.mkstemp(prefix=f"screenshot_{report_id}_", suffix="." + fmt)
    os.close(fd)
    from pathlib import Path as _Path
    _Path(tmp_path).write_bytes(out_bytes)
    snap = python_storage.put_file(
        src_path=tmp_path,
        original_name=name,
        kind="report_screenshot",
        meta={
            "origin": python_storage.make_origin(
                source="report_screenshot",
                file_id=report_id,
                url=report_info.get("url"),
                extra={
                    "title": report_info.get("title"),
                    "format": fmt,
                    "quality": quality,
                    "scale": scale,
                    "crop": crop,
                    "rendered_size": {
                        "width": primitive_result.get("width"),
                        "height": primitive_result.get("height"),
                    },
                    "page_size": {
                        "width": primitive_result.get("full_width"),
                        "height": primitive_result.get("full_height"),
                    },
                    "encoded_size": img.size,
                },
            ),
            "report_id": report_id,
            "image_format": fmt,
        },
        move=True,
    )

    elapsed_s = round(time.time() - started, 2)

    # The orchestrator looks for ``_image`` and converts it into a
    # native image content block for providers that accept one
    # (Anthropic). For text-only providers we still return the file
    # metadata so the LLM at least knows the screenshot exists.
    media_type = "image/webp" if fmt == "webp" else "image/png"
    stored_name = snap["meta"].get("stored_name") or name
    # Relative URL so the frontend (`resolveBackendUrl`) prepends its
    # backend origin. This is what the chat pane's <img src=…> uses;
    # data: URLs would be blocked by the host page's CSP on most large
    # web apps (Drive, Gmail, …) so HTTPS-via-backend is
    # the only path that consistently renders.
    img_url = f"/api/python-storage/{snap['handle']}/{stored_name}"
    return {
        "ok": True,
        "report_id": report_id,
        "title": report_info.get("title"),
        "format": fmt,
        "media_type": media_type,
        "bytes": len(out_bytes),
        "width": img.size[0],
        "height": img.size[1],
        "page_size": {
            "width": primitive_result.get("full_width"),
            "height": primitive_result.get("full_height"),
        },
        "rendered_at_scale": scale,
        "crop_applied": crop,
        # Stage 4.3 telemetry — how many nested three_scene iframes the
        # shim was able to capture via postMessage + canvas.toDataURL.
        # Zero means either (a) the report has no three_scene panes, or
        # (b) WebGL capture failed (timing race, sandbox refused, scene
        # not yet loaded). Compare against the number of three_scene
        # panes the report actually contains to know which.
        "nested_scenes_captured": primitive_result.get("nested_scenes_captured"),
        "handle": snap["handle"],
        "path": snap["path"],
        "url": img_url,
        "elapsed_s": elapsed_s,
        # Internal carrier — orchestrator strips this from the JSON
        # envelope sent to text-only providers and converts it to an
        # image content block for Anthropic. The base64 is the same
        # bytes already on disk; we duplicate here so the orchestrator
        # doesn't have to round-trip through the filesystem. The
        # `url` field is also passed through so the chat pane's rich
        # event uses an HTTPS URL (CSP-safe) instead of the data:
        # URL which most host-page CSPs reject.
        "_image": {
            "media_type": media_type,
            "data": base64.b64encode(out_bytes).decode("ascii"),
            "url": img_url,
        },
    }


registry.register(
    ToolSpec(
        name="screenshot_report",
        description=(
            "Take a screenshot of the HoloViz report currently open in "
            "the iframe pane next to chat, and feed the image into your "
            "context so you can SEE the rendered layout (titles, plot "
            "shapes, axis labels, colour choices, panel arrangement).\n"
            "\n"
            "The screenshot covers the ENTIRE report (full scrollHeight), "
            "not just the visible viewport — html2canvas runs inside "
            "the iframe and rasterises the whole document.\n"
            "\n"
            "Optional `crop` rectangle (percent-of-image coordinates, "
            "all in [0, 100]) lets you focus on a sub-region. Examples:\n"
            "  • whole report:   omit crop entirely\n"
            "  • top half:       {x:0, y:0, width:100, height:50}\n"
            "  • bottom-right "
            "quarter:           {x:50, y:50, width:50, height:50}\n"
            "\n"
            "Output: WebP at quality 80 by default (override `format` "
            "to 'png' for lossless, or `quality` 1-100). Image is "
            "stored in python_storage under `kind: 'report_screenshot'` "
            "so you can refer back to it from compute scripts. The "
            "handle is in the response.\n"
            "\n"
            "If no report is currently open, returns "
            "`{ok: false, error: 'no_report_open'}` — call "
            "`show_holoviz_report(report_id=…)` first.\n"
            "\n"
            "Does NOT work while the report is in edit mode: the "
            "editable template uses modern CSS (color-mix etc.) that "
            "the in-iframe rasteriser cannot parse, and the visible "
            "drag/delete/resize handles would clutter the image. In "
            "edit mode this tool returns `{ok: false, error: "
            "'edit_mode'}` — ask the user to leave edit mode first. "
            "If you only need to know what the user has changed in "
            "edit mode, use `get_report_edits` instead.\n"
            "\n"
            "Provider note: Anthropic ingests the image directly; "
            "OpenAI / Gemini get a text note pointing at the snapshot "
            "handle (those providers' tool-result content blocks are "
            "text-only, so the image surfaces in the chat pane but not "
            "into the LLM's context). Switch to Anthropic in Settings "
            "if visual analysis matters.\n"
            "\n"
            "**Screenshots are LOSSY — known blindnesses:**\n"
            "  • Cross-origin iframes (non-three_scene) appear as blank "
            "    rectangles — html2canvas cannot pierce the sandbox.\n"
            "  • CSS loaded just-in-time may not be applied yet at "
            "    capture time — Tabulator / SlickGrid styles can be "
            "    missing if you screenshot immediately after `ready`.\n"
            "  • Custom webfonts that haven't finished loading fall "
            "    back to system fonts in the capture, even when they "
            "    render correctly live.\n"
            "  • The screenshot is 1–2 animation frames behind on-"
            "    screen state (irrelevant for static content).\n"
            "  • Bokeh WebGL canvases (rare — most plots are 2D) "
            "    come out blank: the buffer is cleared after compositing.\n"
            "\n"
            "Use it for approximate layout verification, NOT pixel-exact "
            "rendering. When you need ground truth, ask the user. For "
            "structural questions ('did I get 3 plots or 2?') prefer "
            "`verify_report` — cheaper and more reliable.\n"
            "\n"
            "**`ctx.three_scene` (WebGL) IS captured.** The shim walks "
            "shadow roots to find each three_scene iframe, asks it for "
            "its canvas pixels via postMessage, and composites the "
            "result. The renderer uses `preserveDrawingBuffer: true` "
            "so the bitmap is readable. Verified end-to-end with both "
            "cold CDN load and re-show. The response includes "
            "`nested_scenes_captured` — non-zero means scenes were "
            "captured. If you see 0 with three_scene panes present, "
            "the scene area in the screenshot will be blank: typically "
            "a timing issue (scene still mid-load); retry in a moment, "
            "or ask the user."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "crop": {
                    "type": "object",
                    "description": (
                        "Optional sub-rectangle in percent-of-image "
                        "coordinates. Omit for whole report."
                    ),
                    "properties": {
                        "x": {"type": "number", "minimum": 0, "maximum": 100},
                        "y": {"type": "number", "minimum": 0, "maximum": 100},
                        "width": {"type": "number", "minimum": 0, "maximum": 100},
                        "height": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["x", "y", "width", "height"],
                    "additionalProperties": False,
                },
                "format": {
                    "type": "string",
                    "enum": ["webp", "png"],
                    "default": "webp",
                    "description": (
                        "Output format. WebP is lossy but ~5x smaller; "
                        "PNG is lossless. Default 'webp' is right for "
                        "almost every case."
                    ),
                },
                "quality": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": _DEFAULT_QUALITY,
                    "description": "WebP quality 1-100 (ignored for PNG).",
                },
                "scale": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 3.0,
                    "default": 1.0,
                    "description": (
                        "Rasterisation scale factor. 1.0 = CSS pixels; "
                        "2.0 doubles resolution (sharper text, ~4x "
                        "bytes). Stay at 1.0 unless you need detail."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_screenshot_report,
        side="hybrid",
    )
)
