"""Patch bokeh_fastapi.WSHandler.send_message to handle a closed-WS race.

Symptom (logged by the asyncio loop as "Task exception was never retrieved"
plus a panel.io.document WARNING)::

    RuntimeError: Unexpected ASGI message 'websocket.send', after sending
    'websocket.close' or response already completed.

Why it happens: ``WSHandler.send_message`` (bokeh_fastapi/handler.py) checks
``self._socket.application_state == CONNECTED`` once at the top, then awaits
several ``send_text`` calls in sequence. Between the guard and any of those
awaits, the client can disconnect — typically when the iframe is unmounted
or reloaded with a new cache-bust ``?_t=…`` URL while Bokeh is mid-push of
the document. uvicorn raises ``RuntimeError`` from
``WebSocketsProtocol.asgi_send`` because the underlying socket has already
been closed; the original ``except WebSocketDisconnect`` block doesn't
match the RuntimeError, so it escapes the task and gets logged as noise.

The fix mirrors the existing WebSocketDisconnect path: invoke ``on_close``
and swallow. The session is gone — the message had nowhere to go; there's
nothing to recover.

Patch is idempotent. Remove this file once bokeh_fastapi handles it
upstream.
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)

_PATCHED_ATTR = "_voitta_send_message_patched"


def patch_send_message() -> None:
    """Wrap ``WSHandler.send_message`` so a closed-WS RuntimeError is
    treated as a graceful disconnect. Safe to call multiple times."""

    from bokeh_fastapi import handler as _bf_handler

    cls = _bf_handler.WSHandler
    if getattr(cls.send_message, _PATCHED_ATTR, False):
        return

    original = cls.send_message

    async def send_message(self: Any, message: Any) -> int:
        try:
            return await original(self, message)
        except RuntimeError as exc:
            text = str(exc)
            # Match uvicorn's specific ASGI-state message. Anything else
            # we re-raise so real bugs aren't swallowed.
            if "websocket.send" in text or "websocket.close" in text:
                try:
                    # 1006 = abnormal closure; the client is gone.
                    self.on_close(1006, "client_disconnected_mid_send")
                except Exception:
                    pass
                log.debug(
                    "bokeh_fastapi WS: client disconnected mid-send (%s)", exc
                )
                return 0
            raise

    setattr(send_message, _PATCHED_ATTR, True)
    cls.send_message = send_message
    log.debug("patched bokeh_fastapi.WSHandler.send_message for closed-WS race")
