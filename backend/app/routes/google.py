"""Routes powering the Google OAuth Settings flow.

* ``GET  /api/google/status``         — configured / connected / email
* ``GET  /api/google/config``         — saved clientId/clientSecret (UI prefill)
* ``POST /api/google/configure``      — persist clientId/clientSecret pair
* ``POST /api/google/disconnect``     — revoke + clear saved tokens
* ``GET  /api/google/oauth/start``    — 302 → Google's consent screen
* ``GET  /api/google/oauth/callback`` — receive code, exchange, store, self-close popup

The flow runs on the localhost backend; the bookmarklet popup hits
``/api/google/oauth/start`` and the callback closes itself once tokens
are persisted. The chat pane polls ``/api/google/status`` to pick up
the new state.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import google_oauth

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/google")


@router.get("/status")
async def google_status() -> dict:
    return google_oauth.status()


@router.get("/config")
async def google_get_config() -> dict:
    """Saved clientId/clientSecret. Localhost-only — same trust
    boundary as ``GET /api/settings`` which already exposes the LLM
    keys via the same socket."""
    return google_oauth.get_client_config()


@router.post("/configure")
async def google_set_config(request: Request) -> dict:
    """Persist a new clientId/clientSecret. If a user was connected,
    revoke + clear the old tokens (they were issued against the old
    client and are no longer valid)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    cid = body.get("clientId")
    csec = body.get("clientSecret")
    try:
        await google_oauth.set_client_credentials(cid or "", csec or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **google_oauth.status()}


@router.post("/disconnect")
async def google_disconnect() -> dict:
    """Revoke + clear saved tokens. Keeps the client_id/client_secret
    (so reconnecting doesn't require re-pasting them)."""
    try:
        await google_oauth.disconnect()
    except Exception as exc:
        _logger.warning("disconnect failed: %s", exc)
    return {"ok": True, **google_oauth.status()}


@router.get("/oauth/start")
async def google_oauth_start():
    """Begin the OAuth dance — redirect the popup to Google's consent
    screen. The widget opens this URL; the callback closes the popup."""
    if not google_oauth.is_configured():
        return HTMLResponse(
            "<h2>Google OAuth not configured</h2>"
            "<p>Open Settings → Google and paste a clientId / clientSecret "
            "first, then retry.</p>",
            status_code=400,
        )
    try:
        url, _state = google_oauth.build_authorize_url()
    except Exception as exc:
        return HTMLResponse(f"<h2>Failed to build auth URL</h2><p>{exc}</p>", status_code=500)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Receive the authorization code, exchange for tokens, store,
    and self-close. The Settings panel polls ``/api/google/status``
    to pick up the new state."""

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
    if not google_oauth.consume_state(state):
        return _close_html(
            "Invalid state",
            "The state token didn't match a pending OAuth request.",
            ok=False,
        )
    try:
        tok = await google_oauth.exchange_code(code)
    except Exception as exc:
        return _close_html("Token exchange failed", str(exc)[:300], ok=False)

    email = tok.get("account_email") or "(unknown)"
    return _close_html(
        "Google Drive connected",
        f"Signed in as <b>{email}</b>. The Drive tools are now available to the LLM.",
        ok=True,
    )
