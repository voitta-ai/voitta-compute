"""HTTP surface for the dynamic model catalog.

* ``GET /api/models/{provider}?force=false`` — resolve a provider's model
  list under the cache-first, snapshot-fallback policy (see
  :mod:`app.services.models_catalog`). ``force=true`` bypasses the TTL and
  refetches when a credential is present.

Returns ``{provider, models, default, source, fetched_at}`` where ``source``
is ``"live" | "cache" | "snapshot"``. Never 500s on a provider outage — the
catalog degrades to last-known cache, then the bundled snapshot.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.services import models_catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/{provider}")
async def get_models(provider: str, force: bool = False) -> dict:
    result = await models_catalog.list_models(provider, force=force)
    return {"provider": provider, **result}
