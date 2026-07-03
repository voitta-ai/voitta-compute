"""Pending session-restore selection for the Claude (subscription) brain.

The history dropdown is in the React app; when the user picks a session to
resume (or starts a new one), the frontend POSTs the choice to an HTTP route —
which records it here, keyed by the current user. The next brain turn consumes
the pending choice and resumes that session (continue-only).

This sidesteps any socket plumbing: the selection is a per-user latch the turn
reads at its start. Keyed by authenticated email (server mode) or a single
local key (desktop). In-process and single-instance by design — a multi-instance
server would back the brain with an external SessionStore instead.
"""

from __future__ import annotations

import threading

_LOCAL_KEY = "__local__"
_lock = threading.Lock()
# email -> (selected: bool, session_id: str | None). ``session_id is None`` with
# selected=True means "start a new conversation" (drop any live resume id).
_pending: dict[str, tuple[bool, str | None]] = {}
# email -> the SDK session id the user's *current* chat pane is on — what the
# history dropdown shows as its label. Set when a turn completes (or a session
# is picked); re-derived from the pending pick at chat start, since a fresh
# pane with nothing pending will start a brand-new session on its next turn.
_active: dict[str, str | None] = {}


def _key(email: str | None) -> str:
    return email or _LOCAL_KEY


def set_pending(email: str | None, session_id: str | None) -> None:
    with _lock:
        _pending[_key(email)] = (True, session_id)


def take_pending(email: str | None) -> tuple[bool, str | None]:
    """Pop the pending selection. Returns ``(selected, session_id)``;
    ``(False, None)`` when nothing is pending."""
    with _lock:
        return _pending.pop(_key(email), (False, None))


def peek_pending(email: str | None) -> tuple[bool, str | None]:
    """Read the pending selection without consuming it."""
    with _lock:
        return _pending.get(_key(email), (False, None))


def set_active(email: str | None, session_id: str | None) -> None:
    with _lock:
        _active[_key(email)] = session_id


def get_active(email: str | None) -> str | None:
    with _lock:
        return _active.get(_key(email))
