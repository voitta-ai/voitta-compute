"""Server-mode login guard — "Sign in with Google".

Distinct from ``app.services.google_oauth`` (that one grants the LLM
Drive/Sheets data access and stores tokens in settings.json). This module
only answers one question: *is the caller a Google account that completed
sign-in?* It mints a short, HMAC-signed session cookie carrying the email;
no Google tokens are stored.

Activation is purely env-driven. The guard is ON iff both
``VOITTA_GOOGLE_AUTH_CLIENT_ID`` and ``VOITTA_GOOGLE_AUTH_CLIENT_SECRET``
are present — which only happens on a server (.env), never in the macOS
bundle or local dev. That is the "server mode only" gate.

Policy for now: anyone who can complete Google sign-in is allowed (no
email allow-list). The redirect URI is ``{VOITTA_PUBLIC_BASE_URL}/api/
auth/google/callback`` and must be registered for the OAuth client in the
Google Cloud console.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import httpx

from app import config

# ---- cookie / session ----------------------------------------------------

COOKIE_NAME = "vc_session"
SESSION_TTL_S = 7 * 24 * 3600  # 7 days

# ---- Google OAuth (openid + email only — we just need identity) ----------

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["openid", "email"]


def is_enabled() -> bool:
    """True iff the login guard is active (server mode). Gated on the
    presence of the dedicated Google login client credentials."""
    return bool(
        os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
        and os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
    )


def _client() -> tuple[str, str]:
    cid = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
    csec = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError("login guard not configured (missing client id/secret)")
    return cid, csec


def redirect_uri() -> str:
    base = (os.getenv("VOITTA_PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/api/auth/google/callback"


# ---- signing secret -------------------------------------------------------

_secret_cache: bytes | None = None


def _secret() -> bytes:
    """HMAC key for session cookies. From ``VOITTA_AUTH_SECRET`` if set,
    else a random value persisted under the data dir so sessions survive
    restarts without manual config."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    env = os.getenv("VOITTA_AUTH_SECRET")
    if env:
        _secret_cache = env.encode("utf-8")
        return _secret_cache
    path = config.USER_DATA_ROOT / "auth_secret"
    try:
        if path.exists():
            val = path.read_bytes().strip()
            if val:
                _secret_cache = val
                return val
        path.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_urlsafe(48).encode("ascii")
        path.write_bytes(val)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _secret_cache = val
        return val
    except Exception:
        # Last resort: ephemeral key (sessions won't survive restart).
        _secret_cache = secrets.token_urlsafe(48).encode("ascii")
        return _secret_cache


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(email: str) -> str:
    """Return a signed ``<payload>.<sig>`` session token for ``email``."""
    payload = {"email": email, "exp": int(time.time()) + SESSION_TTL_S}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_session(token: str | None) -> str | None:
    """Return the email if ``token`` is a valid, unexpired session, else None."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


def email_from_cookie_header(cookie_header: str | None) -> str | None:
    """Parse a raw ``Cookie:`` header and return the authenticated email."""
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return None
    morsel = jar.get(COOKIE_NAME)
    return verify_session(morsel.value) if morsel else None


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
