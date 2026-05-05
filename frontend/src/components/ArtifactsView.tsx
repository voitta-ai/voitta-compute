// Server-side artifacts browser — Finder-like floating window.
//
// Opens from the folder icon in the chat header. Fetches a tree from
// /api/artifacts (python_storage/ + scripts/) and renders it as a
// collapsible tree with expand-all / collapse-all + per-node size
// metadata. Lives inside the shadow root so it floats over the page
// without bleeding global styles.
//
// The window is draggable by the titlebar and resizable from any edge
// or corner. Position + size persist in localStorage across opens.

import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { log } from "../lib/logger";

const STORAGE_RECT_KEY = "voitta-bkmk-artifacts-rect";
const MIN_W = 360;
const MIN_H = 240;
const DEFAULT_W = 720;
const DEFAULT_H = 560;

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

function loadRect(): Rect {
  try {
    const raw = localStorage.getItem(STORAGE_RECT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (
        typeof parsed?.x === "number" &&
        typeof parsed?.y === "number" &&
        typeof parsed?.w === "number" &&
        typeof parsed?.h === "number"
      ) {
        return clampRect(parsed);
      }
    }
  } catch {
    /* ignore */
  }
  // Default: center of viewport with sensible size.
  const w = Math.min(DEFAULT_W, Math.max(MIN_W, window.innerWidth - 80));
  const h = Math.min(DEFAULT_H, Math.max(MIN_H, window.innerHeight - 120));
  return {
    x: Math.max(20, Math.round((window.innerWidth - w) / 2)),
    y: Math.max(20, Math.round((window.innerHeight - h) / 2)),
    w,
    h,
  };
}

function clampRect(r: Rect): Rect {
  const maxW = Math.max(MIN_W, window.innerWidth - 8);
  const maxH = Math.max(MIN_H, window.innerHeight - 8);
  const w = Math.min(maxW, Math.max(MIN_W, r.w));
  const h = Math.min(maxH, Math.max(MIN_H, r.h));
  // Keep at least 60 px of titlebar visible so the window can always be grabbed.
  const x = Math.min(window.innerWidth - 60, Math.max(60 - w, r.x));
  const y = Math.min(window.innerHeight - 40, Math.max(0, r.y));
  return { x, y, w, h };
}

function saveRect(r: Rect): void {
  try {
    localStorage.setItem(STORAGE_RECT_KEY, JSON.stringify(r));
  } catch {
    /* ignore */
  }
}

interface SnapshotInfo {
  handle: string | null;
  kind: string | null;
  display_name: string | null;
  origin: {
    source: string | null;
    account: string | null;
    path: string | null;
    url: string | null;
  } | null;
}

interface ArtifactNode {
  name: string;
  path: string;
  kind: "dir" | "file";
  size: number;
  mtime: string | null;
  child_count?: number;
  children?: ArtifactNode[];
  missing?: boolean;
  snapshot?: SnapshotInfo;
}

interface ArtifactsResponse {
  roots: ArtifactNode[];
  total_size: number;
}

interface Props {
  backendOrigin: string;
  onClose: () => void;
}

function formatSize(bytes: number): string {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatMtime(iso: string | null): string {
  if (!iso) return "";
  // Compact: 2026-05-02 19:32
  return iso.replace("T", " ").replace("Z", "").slice(0, 16);
}

function collectDirPaths(nodes: ArtifactNode[], acc: string[] = []): string[] {
  for (const n of nodes) {
    if (n.kind === "dir") {
      acc.push(n.path);
      if (n.children) collectDirPaths(n.children, acc);
    }
  }
  return acc;
}

function iconFor(node: ArtifactNode): string {
  if (node.kind === "dir") {
    // Snapshot dir gets a more descriptive icon based on its kind.
    const snap = node.snapshot;
    if (snap?.kind === "drive_file") {
      const src = snap.origin?.source || "";
      if (src.startsWith("google_drive")) return "📥";
      return "📥";
    }
    if (snap?.kind === "curves") return "📈";
    return "📁";
  }
  const name = node.name.toLowerCase();
  if (name.endsWith(".py")) return "🐍";
  if (name.endsWith(".json")) return "📋";
  if (name.endsWith(".pkl")) return "📦";
  if (name.endsWith(".png") || name.endsWith(".jpg") || name.endsWith(".jpeg") || name.endsWith(".svg"))
    return "🖼";
  if (name.endsWith(".csv") || name.endsWith(".tsv")) return "📊";
  if (name.endsWith(".pdf")) return "📕";
  return "📄";
}

/** For a list of sibling nodes, return the set of display_names that
 * appear more than once — so we can suffix `(handle)` only when
 * disambiguation is actually needed. */
function collisionDisplayNames(siblings: ArtifactNode[]): Set<string> {
  const counts = new Map<string, number>();
  for (const s of siblings) {
    const dn = s.snapshot?.display_name;
    if (dn) counts.set(dn, (counts.get(dn) || 0) + 1);
  }
  const out = new Set<string>();
  for (const [name, n] of counts) if (n > 1) out.add(name);
  return out;
}

/** Build a one-line origin summary suitable for the row tooltip. */
function originSummary(snap: SnapshotInfo | undefined): string {
  if (!snap || !snap.origin) return "";
  const parts: string[] = [];
  if (snap.origin.source) parts.push(snap.origin.source);
  if (snap.origin.account) parts.push(snap.origin.account);
  if (snap.origin.path) parts.push(snap.origin.path);
  return parts.join(" · ");
}

interface RowProps {
  node: ArtifactNode;
  depth: number;
  expanded: Set<string>;
  toggle: (path: string) => void;
  /** Names that collide among the parent's children — we add a
   * dimmed `(py_xxx)` suffix only for those, not every snapshot. */
  collisionNames: Set<string>;
}

function TreeRow({ node, depth, expanded, toggle, collisionNames }: RowProps) {
  const isOpen = node.kind === "dir" && expanded.has(node.path);
  const isDir = node.kind === "dir";
  const childCount = node.children?.length ?? 0;
  const snap = node.snapshot;

  // Pick what to show as the row label.
  // Snapshot dir → display_name; on collision, suffix with handle.
  // Otherwise → the actual filesystem name.
  let label = node.name;
  let handleSuffix: string | null = null;
  if (snap?.display_name) {
    label = snap.display_name;
    if (snap.handle && collisionNames.has(snap.display_name)) {
      handleSuffix = snap.handle;
    }
  }

  // Tooltip: prefer the origin summary, fallback to the actual path.
  const summary = originSummary(snap);
  const tooltip = summary
    ? `${summary}\n${node.path}`
    : node.path;

  // Children sorted+counted at parent level so we can compute their
  // collision set just once.
  const childCollisions = isDir ? collisionDisplayNames(node.children || []) : null;

  return (
    <>
      <div
        class={`art-row ${isDir ? "art-dir" : "art-file"}`}
        onClick={() => isDir && toggle(node.path)}
        role={isDir ? "button" : undefined}
        title={tooltip}
      >
        <div
          class="art-name-cell"
          style={{ paddingLeft: 8 + depth * 16 + "px" }}
        >
          <span class="art-twisty">{isDir ? (isOpen ? "▾" : "▸") : ""}</span>
          <span class="art-icon" aria-hidden="true">{iconFor(node)}</span>
          <span class="art-name">{label}</span>
          {handleSuffix && (
            <span
              class="art-handle"
              style={{ marginLeft: "6px", opacity: 0.5, fontSize: "0.85em", fontFamily: "monospace" }}
              title={`python_storage handle: ${handleSuffix}`}
            >
              ({handleSuffix})
            </span>
          )}
          {snap?.origin?.source && (
            <span
              class="art-origin"
              style={{ marginLeft: "8px", opacity: 0.55, fontSize: "0.8em" }}
              title={summary}
            >
              {snap.origin.source}{snap.origin.account ? ` · ${snap.origin.account}` : ""}
            </span>
          )}
          {isDir && !snap && (
            <span class="art-badge">
              {childCount}
            </span>
          )}
        </div>
        <span class="art-size">{formatSize(node.size)}</span>
        <span class="art-mtime">{formatMtime(node.mtime)}</span>
      </div>
      {isDir && isOpen && node.children?.map((c) => (
        <TreeRow
          key={c.path}
          node={c}
          depth={depth + 1}
          expanded={expanded}
          toggle={toggle}
          collisionNames={childCollisions || new Set()}
        />
      ))}
    </>
  );
}

type ResizeEdges = {
  n?: boolean;
  s?: boolean;
  e?: boolean;
  w?: boolean;
};

export function ArtifactsView({ backendOrigin, onClose }: Props) {
  const [data, setData] = useState<ArtifactsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rect, setRect] = useState<Rect>(() => loadRect());
  const rectRef = useRef<Rect>(rect);
  rectRef.current = rect;

  // Persist rect changes (debounced via the natural cadence of setRect calls
  // during drag/resize is fine; localStorage writes are cheap for this size).
  useEffect(() => {
    saveRect(rect);
  }, [rect]);

  // Re-clamp on viewport resize so the window doesn't end up off-screen.
  useEffect(() => {
    function onResize() {
      setRect((r) => clampRect(r));
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const startDrag = useCallback((e: PointerEvent) => {
    if (e.button !== 0) return;
    // Don't drag from buttons or the traffic-light dots.
    const target = e.target as HTMLElement;
    if (target.closest("button, .dot")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = rectRef.current;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      setRect(
        clampRect({
          ...rectRef.current,
          x: orig.x + (ev.clientX - startX),
          y: orig.y + (ev.clientY - startY),
        }),
      );
    };
    const up = (ev: PointerEvent) => {
      try {
        el.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      el.removeEventListener("pointermove", move as any);
      el.removeEventListener("pointerup", up as any);
      el.removeEventListener("pointercancel", up as any);
    };
    el.addEventListener("pointermove", move as any);
    el.addEventListener("pointerup", up as any);
    el.addEventListener("pointercancel", up as any);
  }, []);

  const startResize = useCallback((edges: ResizeEdges) => (e: PointerEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = rectRef.current;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      let { x, y, w, h } = orig;
      if (edges.e) w = orig.w + dx;
      if (edges.s) h = orig.h + dy;
      if (edges.w) {
        w = orig.w - dx;
        x = orig.x + dx;
      }
      if (edges.n) {
        h = orig.h - dy;
        y = orig.y + dy;
      }
      // Reverse direction past minimum: anchor the opposite edge.
      if (w < MIN_W) {
        if (edges.w) x = orig.x + (orig.w - MIN_W);
        w = MIN_W;
      }
      if (h < MIN_H) {
        if (edges.n) y = orig.y + (orig.h - MIN_H);
        h = MIN_H;
      }
      setRect(clampRect({ x, y, w, h }));
    };
    const up = (ev: PointerEvent) => {
      try {
        el.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      el.removeEventListener("pointermove", move as any);
      el.removeEventListener("pointerup", up as any);
      el.removeEventListener("pointercancel", up as any);
    };
    el.addEventListener("pointermove", move as any);
    el.addEventListener("pointerup", up as any);
    el.addEventListener("pointercancel", up as any);
  }, []);

  async function fetchArtifacts() {
    setLoading(true);
    setErr(null);
    try {
      const resp = await fetch(backendOrigin + "/api/artifacts", { method: "GET" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const body: ArtifactsResponse = await resp.json();
      setData(body);
      // Default: show the two root dirs expanded so you don't stare at a
      // collapsed list on first open.
      setExpanded(new Set(body.roots.map((r) => r.path)));
    } catch (e: any) {
      log.error("artifacts", "fetch failed", { message: e?.message });
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchArtifacts();
  }, []);

  // Esc closes.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const allDirPaths = useMemo(
    () => (data ? collectDirPaths(data.roots) : []),
    [data],
  );

  function toggle(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function expandAll() {
    setExpanded(new Set(allDirPaths));
  }

  function collapseAll() {
    setExpanded(new Set());
  }

  return (
    <div class="artifacts-overlay">
      <div
        class="artifacts-modal"
        role="dialog"
        aria-label="Server artifacts"
        style={{
          left: rect.x + "px",
          top: rect.y + "px",
          width: rect.w + "px",
          height: rect.h + "px",
        }}
      >
        {/* Resize handles — 4 edges + 4 corners. */}
        <div class="rh rh-n" onPointerDown={startResize({ n: true }) as any} />
        <div class="rh rh-s" onPointerDown={startResize({ s: true }) as any} />
        <div class="rh rh-e" onPointerDown={startResize({ e: true }) as any} />
        <div class="rh rh-w" onPointerDown={startResize({ w: true }) as any} />
        <div class="rh rh-nw" onPointerDown={startResize({ n: true, w: true }) as any} />
        <div class="rh rh-ne" onPointerDown={startResize({ n: true, e: true }) as any} />
        <div class="rh rh-sw" onPointerDown={startResize({ s: true, w: true }) as any} />
        <div class="rh rh-se" onPointerDown={startResize({ s: true, e: true }) as any} />
        <div
          class="artifacts-titlebar"
          onPointerDown={startDrag as any}
          onDblClick={() => {
            // Reset to default centered position+size.
            try {
              localStorage.removeItem(STORAGE_RECT_KEY);
            } catch {
              /* ignore */
            }
            setRect(loadRect());
          }}
          title="Drag to move · double-click to reset"
        >
          <span class="artifacts-traffic">
            <span class="dot dot-red" onClick={onClose} title="Close" />
            <span class="dot dot-yellow" />
            <span class="dot dot-green" />
          </span>
          <span class="artifacts-title">Server artifacts</span>
          <span class="spacer" />
          <button type="button" class="art-btn" onClick={expandAll} title="Expand all">
            Expand all
          </button>
          <button type="button" class="art-btn" onClick={collapseAll} title="Collapse all">
            Collapse all
          </button>
          <button type="button" class="art-btn" onClick={fetchArtifacts} title="Refresh">
            ↻
          </button>
          <button
            type="button"
            class="art-btn art-btn-close"
            onClick={onClose}
            title="Close"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <div class="artifacts-columns">
          <span class="art-col-name">Name</span>
          <span class="art-col-size">Size</span>
          <span class="art-col-mtime">Modified</span>
        </div>
        <div class="artifacts-body">
          {loading && <div class="art-status">Loading…</div>}
          {err && <div class="art-status art-status-err">Error: {err}</div>}
          {!loading && !err && data && (
            <>
              {data.roots.map((r) => (
                <TreeRow
                  key={r.path}
                  node={r}
                  depth={0}
                  expanded={expanded}
                  toggle={toggle}
                  collisionNames={new Set()}
                />
              ))}
            </>
          )}
        </div>
        <div class="artifacts-footer">
          {data && (
            <span>
              {data.roots.length} root{data.roots.length === 1 ? "" : "s"} ·{" "}
              {formatSize(data.total_size)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
