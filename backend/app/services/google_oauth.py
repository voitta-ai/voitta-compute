"""Google OAuth 2.0 — confidential web-app flow for Google API access.

Lives entirely on the backend (token store + token refresh + REST
proxy). Frontend's only role is the Settings panel button that opens
``/api/google/oauth/start`` in a popup; the callback then closes the
window after persisting tokens.

Storage: tokens land in ``settings.googleOAuth.tokens`` of the
backend-owned settings file (``~/.config/voitta-compute/settings.json``,
``0600``). Same store as the LLM API keys — single source of truth.
The OAuth client ID/secret are also there
(``settings.googleOAuth.clientId``, ``…clientSecret``); they're treated
as installation credentials, not per-call settings.

Scope policy: ``drive.readonly`` + ``spreadsheets`` (cell-level read/write
for the Sheets plugin) plus ``openid email`` so we can show the connected
account's email in the UI without an extra API call.

If a user was connected under an older scope set, ``status()`` returns
``needs_reauth: True`` so the UI can prompt them to reconnect.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services import user_settings


# OAuth endpoints
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# What we ask Google for.
# - drive.readonly:  read-only Drive access — no upload, share, or metadata mutation.
# - spreadsheets:    cell-level read/write for the Sheets plugin.
# - openid email:    decode the returned ID token to surface account email in the UI.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Scopes (excluding openid/email) that must be present in a stored token for
# all Google plugins to function. Used by needs_reauth() / status().
_REQUIRED_API_SCOPES = frozenset([
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
])

# Where Google redirects with the authorization code.
# Must match the URI registered for the OAuth client in Google Cloud
# Console exactly.
DEFAULT_REDIRECT_URI = "https://127.0.0.1:12358/api/google/oauth/callback"

# Refresh tokens slightly before expiry so a slow request doesn't
# 401 mid-flight.
REFRESH_GRACE_S = 60.0


# ---- in-memory CSRF state ------------------------------------------------


@dataclass
class _PendingState:
    nonce: str
    created_at: float = field(default_factory=time.time)


# state nonce → bookkeeping. Cleared after callback consumes or after
# 10 min, whichever first.
_pending: dict[str, _PendingState] = {}
_PENDING_TTL_S = 10 * 60

# In-memory token cache — avoids blocking file I/O on the event loop
# every time get_access_token() is called from a coroutine.
_token_cache: dict | None = None


def _invalidate_token_cache() -> None:
    global _token_cache
    _token_cache = None


def _gc_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_S
    for k, v in list(_pending.items()):
        if v.created_at < cutoff:
            _pending.pop(k, None)


# ---- config / store helpers ----------------------------------------------


def _settings() -> dict:
    return user_settings.read()


def _save_oauth_blob(oauth: dict) -> None:
    s = _settings()
    s["googleOAuth"] = oauth
    user_settings.write(s)


def get_client_credentials() -> tuple[str, str] | None:
    """Return ``(client_id, client_secret)`` from settings, or None if
    not configured. Without these the OAuth flow can't start."""
    oauth = _settings().get("googleOAuth") or {}
    cid = oauth.get("clientId")
    csec = oauth.get("clientSecret")
    if isinstance(cid, str) and isinstance(csec, str) and cid and csec:
        return cid, csec
    return None


def get_client_config() -> dict[str, str]:
    """Raw saved client_id/client_secret for the Settings UI prefill.
    Empty strings if not yet set. Localhost-only endpoint, so returning
    the secret value is acceptable — same trust boundary as the LLM
    API keys already exposed via ``GET /api/settings``."""
    oauth = _settings().get("googleOAuth") or {}
    return {
        "clientId": str(oauth.get("clientId") or ""),
        "clientSecret": str(oauth.get("clientSecret") or ""),
    }


async def set_client_credentials(client_id: str, client_secret: str) -> None:
    """Persist a new client_id/client_secret pair.

    If the user was already connected, revoke + clear the existing
    tokens first — they were issued against the OLD client and become
    invalid (or, worse, would silently keep working against the wrong
    project). Single-account, single-credential model: changing creds
    means starting the connection over.
    """
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id is required")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise ValueError("client_secret is required")

    if is_connected():
        await disconnect()

    s = _settings()
    oauth = s.get("googleOAuth") or {}
    oauth["clientId"] = client_id.strip()
    oauth["clientSecret"] = client_secret.strip()
    # disconnect() above already cleared any tokens; be defensive in
    # case the call path ever changes.
    oauth.pop("tokens", None)
    s["googleOAuth"] = oauth
    user_settings.write(s)


def _get_tokens() -> dict | None:
    global _token_cache
    if _token_cache is not None:
        return _token_cache
    oauth = _settings().get("googleOAuth") or {}
    tok = oauth.get("tokens")
    if not isinstance(tok, dict):
        return None
    _token_cache = tok
    return tok


def _set_tokens(tokens: dict) -> None:
    global _token_cache
    _token_cache = tokens
    s = _settings()
    oauth = s.get("googleOAuth") or {}
    oauth["tokens"] = tokens
    s["googleOAuth"] = oauth
    user_settings.write(s)


def _clear_tokens() -> None:
    global _token_cache
    _token_cache = None
    s = _settings()
    oauth = s.get("googleOAuth") or {}
    oauth.pop("tokens", None)
    s["googleOAuth"] = oauth
    user_settings.write(s)


# ---- public state ---------------------------------------------------------


def is_configured() -> bool:
    """True iff client_id + client_secret have been saved. Required
    before the flow can start."""
    return get_client_credentials() is not None


def is_connected() -> bool:
    """True iff we hold a refresh token. Used by tool registration as
    a runtime visibility gate — Drive/Sheets tools are hidden from the
    LLM until the user has connected."""
    tok = _get_tokens()
    return bool(tok and tok.get("refresh_token"))


def has_sheets_scope() -> bool:
    """True iff the stored token includes the Sheets API scope.

    Used as a visibility gate for Sheets tools so they stay hidden if
    the user connected under an older scope set (drive.readonly only)
    and hasn't re-authorised yet.
    """
    tok = _get_tokens()
    if not tok:
        return False
    granted = set((tok.get("scope") or "").split())
    return "https://www.googleapis.com/auth/spreadsheets" in granted


def _needs_reauth() -> bool:
    """True iff connected but the stored token is missing one or more
    required API scopes — user must re-authorise to get them."""
    if not is_connected():
        return False
    tok = _get_tokens()
    granted = set((tok.get("scope") or "").split()) if tok else set()
    return bool(_REQUIRED_API_SCOPES - granted)


def status() -> dict[str, Any]:
    """Status payload for the Settings UI."""
    out: dict[str, Any] = {
        "configured": is_configured(),
        "connected": is_connected(),
        "needs_reauth": _needs_reauth(),
    }
    tok = _get_tokens()
    if tok:
        out["account_email"] = tok.get("account_email")
        out["scopes"] = tok.get("scope", "").split()
        out["expires_in_s"] = max(0, int((tok.get("expires_at") or 0) - time.time()))
    return out


# ---- authorization URL ---------------------------------------------------


def build_authorize_url(redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
    """Return ``(url, state)``. The caller redirects to ``url``; on
    callback they verify ``state`` matches what came back from Google.
    """
    creds = get_client_credentials()
    if creds is None:
        raise RuntimeError("Google OAuth client_id/secret not configured")
    client_id, _ = creds

    state = secrets.token_urlsafe(24)
    _gc_pending()
    _pending[state] = _PendingState(nonce=state)

    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # ``access_type=offline`` requests a refresh token. ``prompt=consent``
        # forces re-consent so a refresh token is RE-issued even if the
        # user already authorised this app once before (Google only
        # includes refresh_token on first consent otherwise).
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def consume_state(state: str) -> bool:
    """Verify and atomically remove ``state``. Returns True if the
    state was a valid pending one."""
    _gc_pending()
    pending = _pending.pop(state, None)
    if pending is None:
        return False
    if time.time() - pending.created_at > _PENDING_TTL_S:
        return False
    return True


# ---- token exchange + refresh --------------------------------------------


async def exchange_code(code: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> dict:
    """Exchange an authorization code for access + refresh tokens.
    Persists the result. Returns the stored token dict."""
    creds = get_client_credentials()
    if creds is None:
        raise RuntimeError("Google OAuth client_id/secret not configured")
    client_id, client_secret = creds

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"token exchange failed {r.status_code}: {r.text[:300]}"
        )
    payload = r.json()

    access_token = payload["access_token"]
    refresh_token = payload.get("refresh_token")
    expires_in = int(payload.get("expires_in", 3600))
    scope = payload.get("scope", "")
    id_token = payload.get("id_token")
    account_email = _email_from_id_token(id_token)

    tok = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
        "scope": scope,
        "account_email": account_email,
        "token_type": payload.get("token_type", "Bearer"),
    }
    _set_tokens(tok)
    return tok


async def get_access_token() -> str:
    """Return a valid access token, refreshing if within ``REFRESH_GRACE_S``
    of expiry. Raises if not connected or refresh fails."""
    tok = _get_tokens()
    if not tok:
        raise RuntimeError("not connected — no Google OAuth tokens stored")
    if time.time() < (tok.get("expires_at") or 0) - REFRESH_GRACE_S:
        return tok["access_token"]
    # Refresh.
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "access token expired and no refresh_token available — "
            "user must re-authorise"
        )
    creds = get_client_credentials()
    if creds is None:
        raise RuntimeError("client_id/secret not configured")
    client_id, client_secret = creds
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"token refresh failed {r.status_code}: {r.text[:300]}"
        )
    payload = r.json()
    new_access = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    # Google's refresh response usually omits refresh_token; keep the
    # original. It also sometimes returns a new id_token.
    id_token = payload.get("id_token")
    if id_token:
        em = _email_from_id_token(id_token)
        if em:
            tok["account_email"] = em
    tok["access_token"] = new_access
    tok["expires_at"] = time.time() + expires_in
    if "scope" in payload:
        tok["scope"] = payload["scope"]
    _set_tokens(tok)
    return new_access


async def disconnect() -> None:
    """Revoke the refresh token (best-effort) and clear local state.

    Revocation tells Google to invalidate the grant — useful so a
    re-connect goes through fresh consent. We don't fail if revoke
    errors (the local clear is what matters for our state)."""
    tok = _get_tokens()
    if tok:
        rt = tok.get("refresh_token") or tok.get("access_token")
        if rt:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(REVOKE_URL, data={"token": rt})
            except Exception:
                pass
    _clear_tokens()


# ---- ID token (account email) -------------------------------------------


def _email_from_id_token(id_token: str | None) -> str | None:
    """Decode the email claim from a Google ID token. We DON'T verify
    the JWT signature — the token came back over a TLS-protected
    request to Google's token endpoint, so the source is trusted; we
    only need to read the payload claim."""
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        import base64
        payload_b64 = parts[1]
        # JWT uses base64url without padding.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        em = payload.get("email")
        return em if isinstance(em, str) else None
    except Exception:
        return None
