"""Model enumeration for the Claude (subscription) brain.

The subscription brain authenticates with an OAuth token (``sk-ant-oat01-…``,
minted by ``claude setup-token``). That same token can call Anthropic's public
**Models API** — ``GET /v1/models`` — which is the *server-driven* source of
truth behind Claude Code's ``/model`` picker: it returns exactly the models the
account is entitled to, newest-first, and updates the moment Anthropic ships a
new model. No hardcoded list, no CLI scraping.

Auth shape (verified 2026-08): the OAuth token goes in ``Authorization: Bearer``
(not ``x-api-key``) together with the ``anthropic-beta: oauth-2025-04-20``
header. Returns HTTP 200 with the full catalog (opus-5 / sonnet-5 / fable-5 /
opus-4-8 …). ``probe_models`` returns ``None`` on any failure so the catalog
falls back to the bundled snapshot.

Historical note: an earlier spike concluded "snapshot-only" because the *CLI*
exposes no enumerate command (``claude models`` hangs interactively). That was
the wrong place to look — the list is an HTTP endpoint, reachable directly with
the stored OAuth token.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Official endpoint. Hardcoded (mirrors app.services.llm.anthropic) so a stray
# ANTHROPIC_BASE_URL in the environment can never redirect this probe.
_MODELS_URL = "https://api.anthropic.com/v1/models"
# Beta flag that authorises an OAuth (subscription) token on the REST API.
_OAUTH_BETA = "oauth-2025-04-20"
_ANTHROPIC_VERSION = "2023-06-01"


async def probe_models() -> list[str] | None:
    """Return the subscription brain's model ids, newest-first, or ``None``.

    Calls ``GET /v1/models`` with the stored OAuth token. Returns ``None`` on a
    missing token, a network/HTTP error, or an unexpected payload — the catalog
    then falls back to the bundled snapshot. Never raises.
    """
    from app.services.agent_sdk.credentials import load_token

    token = load_token()
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": _OAUTH_BETA,
    }
    try:
        import httpx

        collected: list[tuple[str, str]] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Page through in case the catalog ever exceeds one page.
            params: dict[str, str] = {"limit": "1000"}
            while True:
                resp = await client.get(_MODELS_URL, headers=headers, params=params)
                resp.raise_for_status()
                payload = resp.json()
                for model in payload.get("data") or []:
                    mid = model.get("id")
                    if isinstance(mid, str) and mid.startswith("claude-"):
                        collected.append((model.get("created_at") or "", mid))
                if not payload.get("has_more"):
                    break
                last_id = (payload.get("data") or [{}])[-1].get("id")
                if not last_id:
                    break
                params["after_id"] = last_id
    except Exception:
        logger.info("claude_code models probe failed; falling back to snapshot", exc_info=True)
        return None

    if not collected:
        return None
    # API returns newest-first already; sort by created_at desc to be robust.
    collected.sort(key=lambda t: t[0], reverse=True)
    return [mid for _, mid in collected]
