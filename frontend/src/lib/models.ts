// Client for the dynamic model catalog (GET /api/models/{provider}).
//
// The backend owns the cache-first, snapshot-fallback policy; this module is
// a thin fetch + tiny in-memory cache so the Settings dropdown doesn't refetch
// on every render. It never hardcodes a model id — the list and default both
// come from the backend (live data or the bundled snapshot).

import type { ProviderId } from "./settings";

export type ModelSource = "live" | "cache" | "snapshot";

export interface ModelCatalog {
  models: string[];
  default: string;
  source: ModelSource;
  fetched_at: number | null;
}

const EMPTY: ModelCatalog = { models: [], default: "", source: "snapshot", fetched_at: null };

// provider → last-resolved catalog. Populated by fetchModels; read by the UI
// for an instant first paint before the network call lands.
const cache = new Map<ProviderId, ModelCatalog>();

export function getCachedModels(provider: ProviderId): ModelCatalog {
  return cache.get(provider) ?? EMPTY;
}

export function invalidateModels(provider: ProviderId): void {
  cache.delete(provider);
}

/**
 * Resolve a provider's catalog. Returns the backend result and updates the
 * in-memory cache. ``force`` bypasses the backend TTL (used by the ↻ Refresh
 * button). On any network error, returns the last cached catalog (or EMPTY)
 * so the caller never has to handle a throw.
 */
export async function fetchModels(
  backendOrigin: string,
  provider: ProviderId,
  opts: { force?: boolean } = {},
): Promise<ModelCatalog> {
  try {
    const url = `${backendOrigin}/api/models/${provider}${opts.force ? "?force=true" : ""}`;
    const res = await fetch(url, { credentials: "include" });
    if (res.ok) {
      const body = (await res.json()) as Partial<ModelCatalog>;
      const catalog: ModelCatalog = {
        models: Array.isArray(body.models) ? body.models : [],
        default: typeof body.default === "string" ? body.default : "",
        source: (body.source as ModelSource) ?? "snapshot",
        fetched_at: typeof body.fetched_at === "number" ? body.fetched_at : null,
      };
      cache.set(provider, catalog);
      return catalog;
    }
  } catch (err) {
    console.warn("[voitta] model catalog fetch failed", provider, err);
  }
  return cache.get(provider) ?? EMPTY;
}
