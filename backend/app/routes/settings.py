"""GET/PUT /api/settings.

* ``GET``  returns the redacted Settings shape: provider, models, layout,
  theme, ``has_api_keys[provider]: bool`` flags, plus the saved
  ``googleOAuth`` slice (minus tokens) and ``plugins.<name>.<...>`` slice
  for plugin panels to prefill from.
* ``PUT``  accepts either the typed sub-shape (``provider``, ``api_keys``,
  ``models``, ``layout``, ``theme``) OR a ``dotted`` map of
  ``"a.b.c": value`` patches for plugin / Google fields. Both halves can
  appear in the same body — typed fields write via :func:`app.settings.save`,
  the ``dotted`` map via :func:`app.settings.save_dotted`.

API keys remain write-only on the wire: they go in via the typed
``api_keys`` field; they come back only as ``has_api_keys`` booleans.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app import settings as user_settings

router = APIRouter(prefix="/api/settings")


class SettingsPatch(BaseModel):
    provider: str | None = None
    api_keys: dict[str, str] | None = None
    models: dict[str, str] | None = None
    layout: str | None = None
    theme: str | None = None
    max_tool_iterations: int | None = None
    max_tokens: int | None = None
    # Dotted-path patches for plugin / Google config. Values are written
    # verbatim except for ``""`` and ``None`` which DELETE the key.
    dotted: dict[str, Any] | None = None


@router.get("")
async def get_settings() -> dict:
    return user_settings.redacted_for_wire()


@router.put("")
async def put_settings(patch: SettingsPatch) -> dict:
    typed = {
        k: v for k, v in patch.model_dump().items()
        if v is not None and k != "dotted"
    }
    if typed:
        user_settings.save(typed)
    if patch.dotted:
        user_settings.save_dotted(patch.dotted)
    return user_settings.redacted_for_wire()
