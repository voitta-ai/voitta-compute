"""Login-guard routes (server mode). See app.services.login_auth.

All paths under /api/auth/* are intentionally left UNGUARDED by the auth
middleware so the login flow can run before a session exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services import login_auth

router = APIRouter(prefix="/api/auth")


def _set_session_cookie(request: Request, response, email: str) -> None:
    """Mint a Chainlit JWT for ``email`` and set it as the access_token cookie
    using Chainlit's own helper (honours CHAINLIT_COOKIE_SAMESITE=none + the
    shared secret, handles chunking). This is the single identity cookie —
    both our HTTP guard and Chainlit verify it."""
    from chainlit.auth.cookie import set_auth_cookie
    from chainlit.auth.jwt import create_jwt
    from chainlit.user import User

    token = create_jwt(User(identifier=email))
    set_auth_cookie(request, response, token)


def _closer(message: str) -> str:
    """Self-closing popup page. Tells the opener it finished, then closes."""
    safe = message.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>Voitta sign-in</title>"
        "<body style=\"font:14px system-ui;margin:40px;color:#1a1a1a\">"
        f"<p>{safe}</p><p>You can close this window.</p>"
        "<script>"
        "try{if(window.opener)window.opener.postMessage('voitta-auth','*');}catch(e){}"
        "setTimeout(function(){try{window.close();}catch(e){}},600);"
        "</script></body>"
    )


@router.get("/me")
async def me(request: Request) -> dict:
    """Always 200 — the frontend's unguarded probe for auth state."""
    enabled = login_auth.is_enabled()
    email = (
        login_auth.email_from_cookie_header(request.headers.get("cookie"))
        if enabled
        else None
    )
    return {"enabled": enabled, "authenticated": bool(email), "email": email}


@router.get("/google/start")
async def google_start():
    if not login_auth.is_enabled():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    url, _ = login_auth.build_authorize_url()
    return RedirectResponse(url, status_code=302)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if not login_auth.is_enabled():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    if error:
        return HTMLResponse(_closer(f"Sign-in cancelled: {error}"), status_code=400)
    if not code or not state or not login_auth.consume_state(state):
        return HTMLResponse(_closer("Sign-in failed: invalid request."), status_code=400)
    try:
        email = await login_auth.exchange_code(code)
    except Exception:
        return HTMLResponse(_closer("Sign-in failed: token exchange error."), status_code=400)
    if not email:
        return HTMLResponse(_closer("Sign-in failed: no email returned."), status_code=400)

    resp = HTMLResponse(_closer(f"Signed in as {email}."))
    _set_session_cookie(request, resp, email)
    return resp


@router.post("/logout")
async def logout(request: Request):
    from chainlit.auth.cookie import clear_auth_cookie

    resp = JSONResponse({"ok": True})
    clear_auth_cookie(request, resp)
    return resp
