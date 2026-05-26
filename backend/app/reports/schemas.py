"""Wire schemas for the FE.

ONE payload kind: ``html``. The script returns a raw HTML string;
the BE renders + caches it; the FE mounts an iframe of the cached
document. The screenshot shim is injected into ``<head>`` so all
reports are screenshot-capable.

There is no other payload kind. The LLM composes plotly / ELK /
three.js / matplotlib output inside the HTML directly — each via
its own CDN library or by embedding a Python-rendered PNG.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class HtmlPayload(BaseModel):
    """Cached HTML report served from ``/api/html-report``.

    The FE mounts ``<iframe src="/api/html-report?id=...&render_id=...">``
    and the iframe loads the cached body. Same-origin so the shim
    can postMessage measure / reflow / screenshot requests back.
    """

    kind: Literal["html"] = "html"
    url: str
    title: Optional[str] = None


RenderPayload = HtmlPayload


class ShowReportArgs(BaseModel):
    name: str
    title: Optional[str] = None
    render_id: str
    payload: dict[str, Any]
