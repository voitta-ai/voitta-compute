"""Per-request current-user identity + per-user data rooting (server mode).

The login guard (``app.services.login_auth``) verifies a Google account and
mints a signed session cookie. This module carries the resulting email
through the request/turn via a ``ContextVar`` and reroutes all mutable data
paths under a per-user folder so each user only sees their own files.

Activation is implicit: when no email is set (desktop / dev — the guard is
off and nothing populates the contextvar), :func:`user_data_root` returns the
plain ``USER_DATA_ROOT`` and behaviour is byte-for-byte identical to before.
In server mode the authenticated email is set on the contextvar by the HTTP
auth guard (per request) and by the chat socket path (per turn), so data
lands under ``USER_DATA_ROOT/users/<slug>/``.

This is "appearances" isolation: a user running arbitrary Python via
``run_compute`` can still open any path. We scope the normal file paths and
the UI surface, not the interpreter.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import re
from pathlib import Path

from app.config import USER_DATA_ROOT

# The authenticated email for the current request/turn, or None.
_current_email: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "voitta_current_email", default=None
)


def set_current_email(email: str | None):
    """Set the current email; returns a token to pass to :func:`reset_current_email`."""
    return _current_email.set(email)


def reset_current_email(token) -> None:
    try:
        _current_email.reset(token)
    except Exception:
        pass


def get_current_email() -> str | None:
    return _current_email.get()


def email_slug(email: str) -> str:
    """Map an email to a readable, filesystem-safe, collision-free folder name.

    e.g. ``Roman@Voitta.ai`` -> ``roman_at_voitta.ai-1f3a9c2b``. The 8-char
    hash suffix guarantees two distinct emails never share a folder even if
    sanitisation would otherwise flatten them together.
    """
    raw = (email or "").strip().lower()
    readable = raw.replace("@", "_at_")
    readable = re.sub(r"[^a-z0-9._-]", "_", readable)[:48].strip("._-") or "user"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def data_root_for_email(email: str | None) -> Path:
    """Root for *email*'s mutable data, or plain ``USER_DATA_ROOT`` if None."""
    if email:
        return USER_DATA_ROOT / "users" / email_slug(email)
    return USER_DATA_ROOT


def user_data_root() -> Path:
    """Root for the current user's mutable data (from the contextvar).

    Server mode (email set): ``USER_DATA_ROOT/users/<slug>``.
    Desktop / dev (no email): ``USER_DATA_ROOT`` — unchanged.
    """
    return data_root_for_email(_current_email.get())


class UserPath(os.PathLike):
    """A path that re-resolves against the current user on every access.

    Lets the existing module-level ``STORAGE_ROOT`` / ``SCRIPTS_DIR`` style
    constants stay in place: each operation (``/``, ``.mkdir()``,
    ``.iterdir()``, ``.relative_to()`` as an argument, ``str()`` …) delegates
    to a freshly resolved :class:`pathlib.Path`, so the resolved root tracks
    the contextvar without touching any call site.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver):
        # resolver: zero-arg callable returning a concrete Path
        self._resolver = resolver

    def _p(self) -> Path:
        return self._resolver()

    def __truediv__(self, other):
        return self._p() / other

    def __rtruediv__(self, other):
        return other / self._p()

    def __fspath__(self) -> str:
        return str(self._p())

    def __str__(self) -> str:
        return str(self._p())

    def __repr__(self) -> str:
        return f"UserPath({self._p()!r})"

    # Anything not handled above (mkdir, is_dir, iterdir, exists, resolve,
    # glob, relative_to, name, parent, …) delegates to the resolved Path.
    def __getattr__(self, name):
        return getattr(self._p(), name)
