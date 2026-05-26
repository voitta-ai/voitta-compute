"""Helper used by hybrid tools to invoke a browser-side primitive.

Chainlit owns the transport: ``cl.CopilotFunction(name, args).acall()``
round-trips to the React client's ``call_fn`` socket and back. The FE
router ([frontend/src/lib/CallFnRouter.tsx]) looks up ``name`` in the
``primitives`` map ([frontend/src/lib/primitives.ts] — extended by
plugin ``widget.ts`` files via ``registerPrimitive``) and ACKs.

This module wraps that with the same error envelope the source repo
uses, so plugin tools port verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

import chainlit as cl

from app.tools.registry import ToolCtx

logger = logging.getLogger(__name__)


class BrowserToolError(RuntimeError):
    """Raised by :func:`call_browser` when a browser primitive fails."""

    def __init__(self, kind: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


async def call_browser(
    name: str,
    args: dict[str, Any] | None,
    ctx: ToolCtx,
    timeout_ms: int = 15_000,
) -> Any:
    """Invoke browser primitive ``name`` with ``args`` and return its
    payload. The ``ctx`` and ``timeout_ms`` parameters mirror the
    source repo's signature so plugin code ports unchanged — Chainlit
    handles session routing internally, and the timeout is provided
    by Chainlit's own ack-or-fail loop, so we don't enforce it
    separately today.

    Raises :class:`BrowserToolError` when:

    * The Chainlit ``CopilotFunction`` round-trip itself fails (no
      browser attached, socket closed, etc.).
    * The primitive returns a dict with a top-level ``error`` field —
      treated as a structured failure and re-raised as
      ``BrowserToolError`` so the wrapping tool handler can surface
      it to the model uniformly.
    """
    try:
        res = await cl.CopilotFunction(name=name, args=args or {}).acall()
    except Exception as exc:  # noqa: BLE001 — chainlit raises various types
        raise BrowserToolError(
            "dispatch_failed",
            f"browser primitive {name!r} dispatch failed: {exc}",
        ) from exc

    # Some primitives return a `{ error: "..." }` envelope on failure
    # rather than throwing — turn that into a BrowserToolError too so
    # callers don't need two code paths.
    if isinstance(res, dict) and res.get("error") and not res.get("ok", False):
        err = res.get("error")
        kind = "primitive_error"
        message = str(err) if not isinstance(err, dict) else str(err.get("message") or err)
        details: Any = res
        if isinstance(err, dict):
            kind = str(err.get("kind") or kind)
        raise BrowserToolError(kind, message, details)

    return res


__all__ = ["BrowserToolError", "call_browser"]
