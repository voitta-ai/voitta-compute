"""Server-mode login — "Sign in with Google".

Distinct from ``app.services.google_oauth`` (that one grants the LLM
Drive/Sheets data access and stores tokens in settings.json). This module
only answers one question: *is the caller a Google account that completed
sign-in?* On success the callback mints a **Chainlit** JWT (the standard
``access_token`` cookie) carrying the account email as the user identifier.

There is a single source of truth for identity: that Chainlit JWT. Our HTTP
guard (app.main._AuthGuard) verifies the same cookie, and Chainlit verifies
it for /chainlit — so conversation isolation is enforced natively by
Chainlit (per-user threads, ownership checks) with no separate session
cookie of our own.

Activation is purely env-driven. Login is ON iff both
``VOITTA_GOOGLE_AUTH_CLIENT_ID`` and ``VOITTA_GOOGLE_AUTH_CLIENT_SECRET``
are present — which only happens on a server (.env), never in the macOS
bundle or local dev. app.config sets CHAINLIT_CUSTOM_AUTH + the JWT secret
+ SameSite=None in that case, flipping Chainlit's require_login() on.

Policy for now: anyone who can complete Google sign-in is allowed (no
email allow-list). The redirect URI is ``{VOITTA_PUBLIC_BASE_URL}/api/
auth/google/callback`` and must be registered for the OAuth client in the
Google Cloud console.
"""

from __future__ import annotations

import base64
import json
import os
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import httpx

# ---- Google OAuth (openid + email only — we just need identity) ----------

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["openid", "email"]


def is_enabled() -> bool:
    """True iff login is active (server mode). Gated on the presence of the
    dedicated Google login client credentials."""
    return bool(
        os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
        and os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
    )


def _client() -> tuple[str, str]:
    cid = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
    csec = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError("login not configured (missing client id/secret)")
    return cid, csec


def redirect_uri() -> str:
    base = (os.getenv("VOITTA_PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/api/auth/google/callback"


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- identity from the Chainlit JWT cookie --------------------------------


def email_from_cookie_header(cookie_header: str | None) -> str | None:
    """Return the authenticated email by verifying the Chainlit ``access_token``
    JWT in a raw ``Cookie:`` header. Single source of truth for identity,
    shared by the HTTP guard and ``/api/auth/me``. None if absent/invalid."""
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return None
    cookies = {k: m.value for k, m in jar.items()}
    try:
        from chainlit.auth.cookie import get_token_from_cookies
        from chainlit.auth.jwt import decode_jwt

        token = get_token_from_cookies(cookies)
        if not token:
            return None
        user = decode_jwt(token)
        return getattr(user, "identifier", None) or None
    except Exception:
        return None


# ---- OAuth flow -----------------------------------------------------------

# CSRF state nonces → created_at. Consumed by the callback or GC'd after TTL.
_pending: dict[str, float] = {}
_PENDING_TTL_S = 10 * 60


def _gc_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_S
    for k, v in list(_pending.items()):
        if v < cutoff:
            _pending.pop(k, None)


def build_authorize_url() -> tuple[str, str]:
    """Return ``(url, state)`` for the Google consent redirect."""
    cid, _ = _client()
    state = secrets.token_urlsafe(24)
    _gc_pending()
    _pending[state] = time.time()
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def consume_state(state: str) -> bool:
    """Verify + atomically remove a pending CSRF state."""
    _gc_pending()
    created = _pending.pop(state, None)
    return created is not None and (time.time() - created) <= _PENDING_TTL_S


async def exchange_code(code: str) -> str | None:
    """Exchange an auth code for tokens and return the account email."""
    cid, csec = _client()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed {r.status_code}: {r.text[:200]}")
    return _email_from_id_token(r.json().get("id_token"))


def _email_from_id_token(id_token: str | None) -> str | None:
    """Read the email claim from a Google ID token. The JWT signature is
    not verified — it arrived over TLS direct from Google's token endpoint,
    so the source is trusted; we only read the payload."""
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(_unb64(parts[1]).decode("utf-8"))
        email = payload.get("email")
        return email if isinstance(email, str) else None
    except Exception:
        return None
