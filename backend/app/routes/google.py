"""Routes powering the Google OAuth Settings flow (multi-account).

* ``GET    /api/google/status``                   — ordered account list + default
* ``GET    /api/google/config?account=<id>``      — saved label/clientId/clientSecret (UI prefill)
* ``POST   /api/google/accounts``                 — create a named account {label, clientId, clientSecret}
* ``POST   /api/google/accounts/{id}/configure``  — patch label / credentials
* ``POST   /api/google/accounts/{id}/default``    — make this account the default
* ``POST   /api/google/accounts/{id}/disconnect`` — revoke + clear that account's tokens
* ``DELETE /api/google/accounts/{id}``            — revoke + remove the account entry
* ``GET    /api/google/oauth/start?account=<id>`` — 302 → Google's consent screen
* ``GET    /api/google/oauth/callback``           — receive code, exchange, store, self-close popup

The consent popup hits ``oauth/start``; the callback closes itself once
tokens are persisted. The Settings panel polls ``/api/google/status`` to
pick up the new state.

Redirect URI: derived from the live request via ``url_for`` (so server
deployments behind a real hostname work), overridable with the
``GOOGLE_OAUTH_REDIRECT_URI`` env var. The exact value used on the
authorize leg is pinned in the pending OAuth state and repeated on the
token exchange — Google requires an exact match.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import google_oauth
from app.services.current_user import get_current_email
from app.services.google_oauth import UnknownAccount

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/google")


def _redirect_uri(request: Request) -> str:
    """The callback URI for this deployment. Env override wins; else the
    URI is derived from the incoming request so desktop (127.0.0.1) and
    server (real hostname) both register the right value with Google."""
    env = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
    if env:
        return env
    return str(request.url_for("google_oauth_callback"))


@router.get("/status")
async def google_status() -> dict:
    return google_oauth.status()


@router.get("/config")
async def google_get_config(account: str) -> dict:
    """Saved label/clientId/clientSecret for one account. Authenticated /
    localhost-only — same trust boundary as ``GET /api/settings`` which
    already exposes the LLM keys via the same socket."""
    try:
        return google_oauth.get_client_config(account)
    except UnknownAccount as exc:
        raise HTTPException(status_code=404, detail=str(exc))


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body


@router.post("/accounts")
async def google_create_account(request: Request) -> dict:
    """Create a named account entry with its OAuth client credentials."""
    body = await _json_body(request)
    try:
        account_id = google_oauth.create_account(
            str(body.get("label") or ""),
            str(body.get("clientId") or ""),
            str(body.get("clientSecret") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": account_id, **google_oauth.status()}


@router.post("/accounts/{account_id}/configure")
async def google_configure_account(account_id: str, request: Request) -> dict:
    """Patch label / credentials. Changing credentials revokes + clears
    that account's tokens (they were issued against the old client)."""
    body = await _json_body(request)
    kwargs: dict = {}
    if "label" in body:
        kwargs["label"] = str(body.get("label") or "")
    if "clientId" in body:
        kwargs["client_id"] = str(body.get("clientId") or "")
    if "clientSecret" in body:
        kwargs["client_secret"] = str(body.get("clientSecret") or "")
    if not kwargs:
        raise HTTPException(status_code=400, detail="nothing to update")
    try:
        await google_oauth.update_account(account_id, **kwargs)
    except UnknownAccount as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **google_oauth.status()}


@router.post("/accounts/{account_id}/default")
async def google_set_default(account_id: str) -> dict:
    try:
        google_oauth.set_default_account(account_id)
    except UnknownAccount as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, **google_oauth.status()}


@router.post("/accounts/{account_id}/disconnect")
async def google_disconnect(account_id: str) -> dict:
    """Revoke + clear one account's tokens. Keeps the credentials so
    reconnecting doesn't require re-pasting them."""
    try:
        await google_oauth.disconnect(account_id)
    except Exception as exc:
        _logger.warning("disconnect(%s) failed: %s", account_id, exc)
    return {"ok": True, **google_oauth.status()}


@router.delete("/accounts/{account_id}")
async def google_delete_account(account_id: str) -> dict:
    try:
        await google_oauth.delete_account(account_id)
    except UnknownAccount as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, **google_oauth.status()}


@router.get("/oauth/start")
async def google_oauth_start(request: Request, account: str):
    """Begin the OAuth dance for one account — redirect the popup to
    Google's consent screen. The callback closes the popup."""
    if not google_oauth.is_configured(account):
        return HTMLResponse(
            "<h2>Google OAuth not configured</h2>"
            "<p>Open Settings → Google, add this account's clientId / "
            "clientSecret first, then retry.</p>",
            status_code=400,
        )
    try:
        url, _state = google_oauth.build_authorize_url(
            account,
            redirect_uri=_redirect_uri(request),
            user_email=get_current_email(),
        )
    except Exception as exc:
        return HTMLResponse(f"<h2>Failed to build auth URL</h2><p>{exc}</p>", status_code=500)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Receive the authorization code, exchange for tokens for the
    account pinned in the OAuth state, store, and self-close."""

    def _close_html(title: str, body: str, ok: bool) -> HTMLResponse:
        color = "#0a8a3a" if ok else "#b00020"
        return HTMLResponse(
            f"""<!doctype html><html><head><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif;
         color: #222; background: #fafafa;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; }}
  .card {{ background: white; padding: 28px 32px; border-radius: 8px;
         box-shadow: 0 1px 3px rgba(0,0,0,0.08); max-width: 420px;
         text-align: center; }}
  h2 {{ margin: 0 0 8px; font-size: 18px; color: {color}; }}
</style></head>
<body><div class="card"><h2>{title}</h2><p>{body}</p>
<p style="color:#888;font-size:12px;">You can close this window.</p>
</div>
<script>setTimeout(function(){{ try {{ window.close(); }} catch(e){{}} }}, 1500);</script>
</body></html>"""
        )

    if error:
        return _close_html("Connection cancelled", f"Google returned: <code>{error}</code>.", ok=False)
    if not code or not state:
        return _close_html("Bad callback", "Missing code/state.", ok=False)
    pending = google_oauth.consume_state(state)
    if pending is None:
        return _close_html(
            "Invalid state",
            "The state token didn't match a pending OAuth request.",
            ok=False,
        )
    # Server mode: the callback must land on the same user's settings
    # file the flow started from. Both are None on desktop.
    if pending.user_email != get_current_email():
        return _close_html(
            "Session mismatch",
            "This OAuth flow was started by a different login session. "
            "Retry Connect from Settings.",
            ok=False,
        )
    try:
        tok = await google_oauth.exchange_code(
            code, pending.account_id, redirect_uri=pending.redirect_uri,
        )
    except Exception as exc:
        return _close_html("Token exchange failed", str(exc)[:300], ok=False)

    email = tok.get("account_email") or "(unknown)"
    label = google_oauth.account_label(pending.account_id)
    return _close_html(
        "Google account connected",
        f"Signed in as <b>{email}</b> ({label}). The Drive tools are now "
        "available to the LLM.",
        ok=True,
    )
