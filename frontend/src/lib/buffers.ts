// Browser-side data buffers.
//
// Holds bulk payloads (parser previews, dataset rows, query results) so they
// don't have to round-trip through the LLM context window. The model
// receives only the *handle* + a small *summary*; consumer tools (query /
// plot / eval) accept the handle and resolve it locally.
//
// Storage is in-memory only. A page reload clears everything. We
// deliberately avoid sessionStorage — its 5 MB cap is too small, and
// JSON-stringifying every buffer on write is wasteful when most reads are
// just len/structural.
//
// No size cap by default. Buffers stay until explicitly deleted, the user
// calls `buffer_clear`, or the page reloads. The previous FIFO-evicting
// 200 MB cap silently dropped buffers the model was still using, breaking
// later plot_*_from_buffer calls; sampling at fetch time is the right way
// to keep memory bounded.

export interface BufferRecord {
  handle: string;
  kind: string;
  data: unknown;
  bytes: number;
  summary: unknown;
  createdAt: string;
  meta: Record<string, unknown>;
}

export interface BufferInfo {
  handle: string;
  kind: string;
  bytes: number;
  summary: unknown;
  createdAt: string;
  meta: Record<string, unknown>;
}

const buffers = new Map<string, BufferRecord>();
let counter = 0;
let totalBytes = 0;

function newHandle(): string {
  counter += 1;
  const r = Math.random().toString(36).slice(2, 6);
  return `buf_${r}${counter.toString(36)}`;
}

function approxBytes(v: unknown): number {
  try {
    return JSON.stringify(v).length;
  } catch {
    return 0;
  }
}

export function bufferPut(
  value: unknown,
  kind: string,
  summary: unknown,
  meta: Record<string, unknown> = {},
): BufferRecord {
  const bytes = approxBytes(value);
  const handle = newHandle();
  const rec: BufferRecord = {
    handle,
    kind,
    data: value,
    bytes,
    summary,
    createdAt: new Date().toISOString(),
    meta,
  };
  buffers.set(handle, rec);
  totalBytes += bytes;
  return rec;
}

export function bufferGet(handle: string): BufferRecord | null {
  return buffers.get(handle) || null;
}

export function bufferDelete(handle: string): boolean {
  const rec = buffers.get(handle);
  if (!rec) return false;
  totalBytes -= rec.bytes;
  buffers.delete(handle);
  return true;
}

export function bufferClear(): { freed_count: number; freed_bytes: number } {
  const freed_count = buffers.size;
  const freed_bytes = totalBytes;
  buffers.clear();
  totalBytes = 0;
  return { freed_count, freed_bytes };
}

export function bufferList(): BufferInfo[] {
  return [...buffers.values()].map((b) => ({
    handle: b.handle,
    kind: b.kind,
    bytes: b.bytes,
    summary: b.summary,
    createdAt: b.createdAt,
    meta: b.meta,
  }));
}

export function bufferTotals(): { count: number; bytes: number } {
  return { count: buffers.size, bytes: totalBytes };
}

// Partial delete: drop a list of dot-paths from a buffer's data. Useful for
// shedding heavy series you don't need anymore without re-fetching the
// whole buffer. Path syntax mirrors lodash-style: `data.curves[5].series[1].values`.
//
// Returns the bytes freed (computed by re-summarising) plus the paths
// that were actually present. Missing paths are silently no-op'd; the
// caller can compare `requested` to `dropped` to diagnose typos.
export function bufferDeleteKeys(
  handle: string,
  paths: string[],
): {
  ok: boolean;
  dropped: string[];
  not_found: string[];
  bytes_before: number;
  bytes_after: number;
} {
  const rec = buffers.get(handle);
  if (!rec) {
    return {
      ok: false,
      dropped: [],
      not_found: paths,
      bytes_before: 0,
      bytes_after: 0,
    };
  }
  const before = rec.bytes;
  const dropped: string[] = [];
  const notFound: string[] = [];
  for (const p of paths) {
    if (deleteAtPath(rec.data, p)) {
      dropped.push(p);
    } else {
      notFound.push(p);
    }
  }
  rec.bytes = approxBytes(rec.data);
  totalBytes += rec.bytes - before;
  return {
    ok: true,
    dropped,
    not_found: notFound,
    bytes_before: before,
    bytes_after: rec.bytes,
  };
}

// Walk a dot/bracket path and `delete` the leaf. Returns true if the leaf
// existed and was removed.
function deleteAtPath(root: unknown, path: string): boolean {
  // Tokenise: foo.bar[3].baz -> ['foo', 'bar', 3, 'baz']
  const tokens: (string | number)[] = [];
  let i = 0;
  while (i < path.length) {
    if (path[i] === ".") {
      i++;
      continue;
    }
    if (path[i] === "[") {
      const j = path.indexOf("]", i);
      if (j < 0) return false;
      tokens.push(parseInt(path.slice(i + 1, j), 10));
      i = j + 1;
      continue;
    }
    let j = i;
    while (j < path.length && path[j] !== "." && path[j] !== "[") j++;
    tokens.push(path.slice(i, j));
    i = j;
  }
  if (!tokens.length) return false;

  let node: any = root;
  for (let k = 0; k < tokens.length - 1; k++) {
    if (node == null) return false;
    node = node[tokens[k] as any];
  }
  const last = tokens[tokens.length - 1];
  if (node == null) return false;
  if (typeof last === "number" && Array.isArray(node)) {
    if (last < 0 || last >= node.length) return false;
    node[last] = undefined; // hole, preserves indices
    return true;
  }
  if (typeof last === "string" && typeof node === "object") {
    if (!(last in node)) return false;
    delete node[last];
    return true;
  }
  return false;
}

// ---- summarizers ----------------------------------------------------------

export function summarizeMeasurementCurves(payload: unknown): unknown {
  const curves: any[] = Array.isArray((payload as any)?.curves)
    ? (payload as any).curves
    : Array.isArray(payload)
      ? (payload as any[])
      : [];
  const namesByCount: Record<string, number> = {};
  const seriesNames = new Set<string>();
  let exampleCurve: unknown = null;

  for (const c of curves) {
    const meta: any[] = c.metadata || [];
    const nameMeta = meta.find((m: any) => m.key === "Name");
    const name = nameMeta?.value || c.name || "(unknown)";
    namesByCount[name] = (namesByCount[name] || 0) + 1;

    for (const s of c.series || []) {
      if (s?.name) seriesNames.add(s.name);
    }

    if (!exampleCurve) {
      exampleCurve = {
        id: c.id,
        name: c.name,
        metadata: Object.fromEntries(meta.map((m: any) => [m.key, m.value])),
        seriesCount: (c.series || []).length,
        series: (c.series || []).map((s: any) => ({
          name: s.name,
          unit: s.unit,
          measurand: s.measurand,
          valuesLength: Array.isArray(s.values) ? s.values.length : 0,
        })),
      };
    }
  }

  return {
    curveCount: curves.length,
    curveNamesByCount: namesByCount,
    seriesNames: [...seriesNames].sort(),
    exampleCurve,
  };
}

export function summarizeGeneric(payload: unknown): unknown {
  if (Array.isArray(payload)) {
    return {
      kind: "array",
      length: payload.length,
      sample: payload.slice(0, 1)[0] ?? null,
    };
  }
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    const keys = Object.keys(obj);
    const summary: Record<string, unknown> = { kind: "object", keys };
    for (const k of keys.slice(0, 12)) {
      const v = obj[k];
      if (Array.isArray(v)) summary[`${k}_length`] = v.length;
      else if (v && typeof v === "object") summary[`${k}_keys`] = Object.keys(v).length;
    }
    return summary;
  }
  return { kind: typeof payload, value: payload };
}

// ---- helpers used by primitives & worker --------------------------------

export function getMetaValue(curve: any, key: string): string | null {
  const m = (curve?.metadata || []).find((x: any) => x.key === key);
  return m ? m.value : null;
}

export function filterCurves(curves: any[], filter: Record<string, string>): any[] {
  if (!filter || !Object.keys(filter).length) return curves;
  return curves.filter((c) => {
    for (const [k, v] of Object.entries(filter)) {
      if (getMetaValue(c, k) !== v) return false;
    }
    return true;
  });
}

export function curveSeries(curve: any, name: string): number[] | null {
  const s = (curve?.series || []).find((x: any) => x.name === name);
  return s ? (Array.isArray(s.values) ? s.values : null) : null;
}

export function curvesPayload(buffer: BufferRecord): any[] {
  const data: any = buffer.data;
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.curves)) return data.curves;
  return [];
}
