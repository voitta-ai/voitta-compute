// Server-side artifacts browser — Finder-like floating window.
//
// Opens from the folder icon in the chat header. Fetches a tree from
// /api/artifacts (python_storage/ + scripts/) and renders it as a
// collapsible tree with Finder-grade interactions:
//
//   • Single click       — select that row, clear others.
//   • Cmd+click          — toggle that row in the selection.
//   • Shift+click        — range-select from the anchor.
//   • Right-click        — opens a context menu; if the row isn't already
//                          selected, replace selection first (Finder
//                          behavior).
//   • Delete / ⌘⌫         — delete the selection when all rows are
//                          deletable units.
//   • Enter (single row) — start rename when the row is renameable.
//
// Context-menu actions (gated by selection):
//   Open                  — single file → opens in a new tab via the
//                          existing /api/python-storage or /api/script-output
//                          URL.
//   Run                   — single reports/<slug> or flows/<slug> → calls
//                          POST /api/artifacts/<path>/run and hands the
//                          result to show_report / show_flow_report.
//   Reveal handle         — single snapshot dir → copies py_xxx.
//   Rename                — single deletable unit (not run dirs).
//   Delete                — selection is all-deletable.
//
// The lives inside the shadow root so it floats over the page
// without bleeding global styles. Position + size persist in localStorage.

import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { invokePrimitive } from "../lib/bridge";
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

// ─── Unit classification (mirrors backend allow-list) ──────────────────────
//
// Same rules as backend `_classify_artifact_path`. The frontend needs them
// to gate context-menu items and shortcuts; backend is the authority and
// re-validates on every mutation request.

type UnitKind = "snapshot" | "compute" | "reports" | "flows" | "run" | null;

function classifyPath(p: string): UnitKind {
  if (/^python_storage\/cache\/snapshot_[A-Za-z0-9_-]+$/.test(p)) return "snapshot";
  if (/^python_storage\/compute\/[a-z0-9_-]{1,64}\/runs\/[A-Za-z0-9_-]{4,64}$/.test(p))
    return "run";
  const m = /^python_storage\/(compute|reports|flows)\/[a-z0-9_-]{1,64}$/.exec(p);
  if (m) return m[1] as UnitKind;
  return null;
}

function isDeletable(p: string): boolean {
  return classifyPath(p) !== null;
}

function isRenameable(p: string): boolean {
  const k = classifyPath(p);
  return k !== null && k !== "run";
}

function isRunnable(p: string): boolean {
  const k = classifyPath(p);
  return k === "reports" || k === "flows";
}

// ─── Utility ───────────────────────────────────────────────────────────────

function formatSize(bytes: number): string {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatMtime(iso: string | null): string {
  if (!iso) return "";
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

/** Walk the tree honoring ``expanded`` and return the flat sequence of
 * paths the user can actually see. Used by Shift+click range selection
 * and ↑/↓ navigation so they follow visual order, not tree order. */
function flattenVisible(
  nodes: ArtifactNode[],
  expanded: Set<string>,
  acc: string[] = [],
): string[] {
  for (const n of nodes) {
    acc.push(n.path);
    if (n.kind === "dir" && expanded.has(n.path) && n.children) {
      flattenVisible(n.children, expanded, acc);
    }
  }
  return acc;
}

function findNode(nodes: ArtifactNode[], path: string): ArtifactNode | null {
  for (const n of nodes) {
    if (n.path === path) return n;
    if (n.children) {
      const found = findNode(n.children, path);
      if (found) return found;
    }
  }
  return null;
}

function iconFor(node: ArtifactNode): string {
  if (node.kind === "dir") {
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

function originSummary(snap: SnapshotInfo | undefined): string {
  if (!snap || !snap.origin) return "";
  const parts: string[] = [];
  if (snap.origin.source) parts.push(snap.origin.source);
  if (snap.origin.account) parts.push(snap.origin.account);
  if (snap.origin.path) parts.push(snap.origin.path);
  return parts.join(" · ");
}

// ─── Context menu ──────────────────────────────────────────────────────────

interface MenuItem {
  label: string;
  shortcut?: string;
  disabled?: boolean;
  onClick?: () => void;
}

interface MenuState {
  x: number;
  y: number;
  items: MenuItem[];
}

function ContextMenu({ state, onDismiss }: { state: MenuState; onDismiss: () => void }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    function onDown(e: MouseEvent) {
      // Shadow-DOM retargeting: ``e.target`` is the shadow host, not the
      // menu div. Walk ``composedPath()`` to detect whether the click is
      // actually inside the menu element. Without this, every click on a
      // menu item dismisses the menu BEFORE the item's onClick fires —
      // looks like the action did nothing.
      const path = typeof (e as any).composedPath === "function"
        ? ((e as any).composedPath() as EventTarget[])
        : [];
      const inside = ref.current && path.includes(ref.current);
      if (!inside) onDismiss();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onDismiss();
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onDismiss]);

  // Keep within the modal bounds.
  const style: any = {
    left: state.x + "px",
    top: state.y + "px",
  };

  return (
    <div class="art-ctxmenu" style={style} ref={ref} role="menu">
      {state.items.map((it, i) =>
        it.label === "---" ? (
          <div class="art-ctxmenu-sep" key={"sep-" + i} />
        ) : (
          <div
            class={`art-ctxmenu-item${it.disabled ? " is-disabled" : ""}`}
            key={i}
            role="menuitem"
            aria-disabled={it.disabled || undefined}
            onMouseDown={(e) => {
              // Fire on mousedown — by the time `click` would fire, our
              // document-level mousedown listener has already unmounted us
              // (it sees a *different* path because the shadow-root walk
              // may miss a transient hit). Mousedown on this element runs
              // FIRST in the event order, so we both invoke the action and
              // stop propagation before the dismissal handler sees it.
              e.preventDefault();
              e.stopPropagation();
              log.info("artifacts.menu", "item mousedown", { label: it.label, disabled: !!it.disabled });
              if (it.disabled) return;
              try { it.onClick?.(); }
              catch (err: any) { log.error("artifacts.menu", "item handler threw", { label: it.label, message: err?.message }); }
              onDismiss();
            }}
            onClick={(e) => {
              // Belt-and-braces: if mousedown didn't fire for some reason
              // (touch, etc.), this is the fallback path.
              e.preventDefault();
              e.stopPropagation();
            }}
          >
            <span class="art-ctxmenu-label">{it.label}</span>
            {it.shortcut && <span class="art-ctxmenu-shortcut">{it.shortcut}</span>}
          </div>
        ),
      )}
    </div>
  );
}

// ─── Inline rename ─────────────────────────────────────────────────────────

function RenameInput({
  initial,
  validate,
  onCommit,
  onCancel,
}: {
  initial: string;
  /** Returns null if ``v`` is valid, otherwise an error message. The
   * input flashes red and stays in edit mode when validation fails. */
  validate?: (v: string) => string | null;
  onCommit: (next: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement | null>(null);
  // ``Enter`` triggers commit, which sets renaming=null and unmounts us.
  // The unmount blurs the input, which would fire commit AGAIN. Dedupe.
  const firedRef = useRef(false);

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  function commit() {
    if (firedRef.current) return;
    const next = value.trim();
    if (!next || next === initial) {
      firedRef.current = true;
      onCancel();
      return;
    }
    if (validate) {
      const msg = validate(next);
      if (msg) {
        setError(msg);
        return;             // stay in edit mode
      }
    }
    firedRef.current = true;
    onCommit(next);
  }

  return (
    <input
      ref={ref}
      class={`art-rename-input${error ? " is-invalid" : ""}`}
      value={value}
      title={error || undefined}
      onInput={(e) => {
        setValue((e.target as HTMLInputElement).value);
        if (error) setError(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        } else if (e.key === "Escape") {
          e.preventDefault();
          firedRef.current = true;
          onCancel();
        }
        e.stopPropagation();
      }}
      onBlur={commit}
      onClick={(e) => e.stopPropagation()}
    />
  );
}

// ─── Tree row ──────────────────────────────────────────────────────────────

interface RowProps {
  node: ArtifactNode;
  depth: number;
  expanded: Set<string>;
  selected: Set<string>;
  renaming: string | null;
  collisionNames: Set<string>;
  onToggle: (path: string) => void;
  onClick: (path: string, e: MouseEvent) => void;
  onContextMenu: (path: string, e: MouseEvent) => void;
  onRenameCommit: (path: string, next: string) => void;
  onRenameCancel: () => void;
}

function TreeRow(props: RowProps) {
  const {
    node, depth, expanded, selected, renaming,
    collisionNames, onToggle, onClick, onContextMenu,
    onRenameCommit, onRenameCancel,
  } = props;
  const isOpen = node.kind === "dir" && expanded.has(node.path);
  const isDir = node.kind === "dir";
  const childCount = node.children?.length ?? 0;
  const snap = node.snapshot;
  const isSelected = selected.has(node.path);
  const isRenaming = renaming === node.path;

  let label = node.name;
  let handleSuffix: string | null = null;
  if (snap?.display_name) {
    label = snap.display_name;
    if (snap.handle && collisionNames.has(snap.display_name)) {
      handleSuffix = snap.handle;
    }
  }

  const summary = originSummary(snap);
  const tooltip = summary ? `${summary}\n${node.path}` : node.path;
  const childCollisions = isDir ? collisionDisplayNames(node.children || []) : null;

  function handleClick(e: MouseEvent) {
    if (isRenaming) return;
    // Twisty (chevron) area: toggle without disturbing selection.
    const target = e.target as HTMLElement;
    if (target.classList.contains("art-twisty")) {
      onToggle(node.path);
      return;
    }
    onClick(node.path, e);
  }

  function handleDblClick(e: MouseEvent) {
    if (isRenaming) return;
    if (isDir) {
      onToggle(node.path);
    }
    e.stopPropagation();
  }

  return (
    <>
      <div
        class={`art-row ${isDir ? "art-dir" : "art-file"}${isSelected ? " is-selected" : ""}`}
        onClick={handleClick}
        onDblClick={handleDblClick}
        onContextMenu={(e) => {
          e.preventDefault();
          onContextMenu(node.path, e);
        }}
        role={isDir ? "button" : undefined}
        title={tooltip}
      >
        <div class="art-name-cell" style={{ paddingLeft: 8 + depth * 16 + "px" }}>
          <span class="art-twisty">{isDir ? (isOpen ? "▾" : "▸") : ""}</span>
          <span class="art-icon" aria-hidden="true">{iconFor(node)}</span>
          {isRenaming ? (
            <RenameInput
              initial={label}
              validate={(v) => {
                // For script slugs, the dir is renamed on disk — enforce
                // the backend's regex client-side so the user gets instant
                // feedback (the backend's 400 surfaces as an alert that
                // dismisses edit mode, which is worse UX).
                const kind = classifyPath(node.path);
                if (kind === "compute" || kind === "reports" || kind === "flows") {
                  if (!/^[a-z0-9_-]{1,64}$/.test(v)) {
                    return "lowercase letters, digits, dash, underscore (max 64)";
                  }
                }
                return null;
              }}
              onCommit={(next) => onRenameCommit(node.path, next)}
              onCancel={onRenameCancel}
            />
          ) : (
            <span class="art-name">{label}</span>
          )}
          {!isRenaming && handleSuffix && (
            <span
              class="art-handle"
              style={{ marginLeft: "6px", opacity: 0.5, fontSize: "0.85em", fontFamily: "monospace" }}
              title={`python_storage handle: ${handleSuffix}`}
            >
              ({handleSuffix})
            </span>
          )}
          {!isRenaming && snap?.origin?.source && (
            <span
              class="art-origin"
              style={{ marginLeft: "8px", opacity: 0.55, fontSize: "0.8em" }}
              title={summary}
            >
              {snap.origin.source}{snap.origin.account ? ` · ${snap.origin.account}` : ""}
            </span>
          )}
          {!isRenaming && isDir && !snap && (
            <span class="art-badge">{childCount}</span>
          )}
        </div>
        <span class="art-size">{formatSize(node.size)}</span>
        <span class="art-mtime">{formatMtime(node.mtime)}</span>
      </div>
      {isDir && isOpen && node.children?.map((c) => (
        <TreeRow
          {...props}
          key={c.path}
          node={c}
          depth={depth + 1}
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

// ─── Main view ─────────────────────────────────────────────────────────────

export function ArtifactsView({ backendOrigin, onClose }: Props) {
  const [data, setData] = useState<ArtifactsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [rect, setRect] = useState<Rect>(() => loadRect());
  const rectRef = useRef<Rect>(rect);
  rectRef.current = rect;

  useEffect(() => { saveRect(rect); }, [rect]);

  useEffect(() => {
    function onResize() {
      setRect((r) => clampRect(r));
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const startDrag = useCallback((e: PointerEvent) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest("button, .dot")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = rectRef.current;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      setRect(clampRect({
        ...rectRef.current,
        x: orig.x + (ev.clientX - startX),
        y: orig.y + (ev.clientY - startY),
      }));
    };
    const up = (ev: PointerEvent) => {
      try { el.releasePointerCapture(ev.pointerId); } catch { /* ignore */ }
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
      if (edges.w) { w = orig.w - dx; x = orig.x + dx; }
      if (edges.n) { h = orig.h - dy; y = orig.y + dy; }
      if (w < MIN_W) { if (edges.w) x = orig.x + (orig.w - MIN_W); w = MIN_W; }
      if (h < MIN_H) { if (edges.n) y = orig.y + (orig.h - MIN_H); h = MIN_H; }
      setRect(clampRect({ x, y, w, h }));
    };
    const up = (ev: PointerEvent) => {
      try { el.releasePointerCapture(ev.pointerId); } catch { /* ignore */ }
      el.removeEventListener("pointermove", move as any);
      el.removeEventListener("pointerup", up as any);
      el.removeEventListener("pointercancel", up as any);
    };
    el.addEventListener("pointermove", move as any);
    el.addEventListener("pointerup", up as any);
    el.addEventListener("pointercancel", up as any);
  }, []);

  /** Refetch the tree. ``initial=true`` only on first open / explicit
   * Refresh — that's when we seed expanded=roots and clear selection.
   * Mutation refetches (after rename/delete) preserve whatever the user
   * had expanded and selected, the way Finder leaves the view stable
   * across in-place file ops. Paths that no longer exist drop out of
   * both sets naturally on the next render — they just no-op. */
  async function fetchArtifacts(initial = false) {
    setLoading(true);
    setErr(null);
    try {
      const resp = await fetch(backendOrigin + "/api/artifacts", {
        method: "GET",
        credentials: "include",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const body: ArtifactsResponse = await resp.json();
      setData(body);
      if (initial) {
        setExpanded(new Set(body.roots.map((r) => r.path)));
        setSelected(new Set());
        setAnchor(null);
      }
    } catch (e: any) {
      log.error("artifacts", "fetch failed", { message: e?.message });
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchArtifacts(true); }, []);

  const visiblePaths = useMemo(
    () => (data ? flattenVisible(data.roots, expanded) : []),
    [data, expanded],
  );

  const allDirPaths = useMemo(
    () => (data ? collectDirPaths(data.roots) : []),
    [data],
  );

  function toggle(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  }

  function expandAll() { setExpanded(new Set(allDirPaths)); }
  function collapseAll() { setExpanded(new Set()); }

  // ─── Selection ───────────────────────────────────────────────────────────

  function selectOnly(path: string) {
    setSelected(new Set([path]));
    setAnchor(path);
  }

  function selectToggle(path: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
    setAnchor(path);
  }

  function selectRange(path: string) {
    if (!anchor) return selectOnly(path);
    const i = visiblePaths.indexOf(anchor);
    const j = visiblePaths.indexOf(path);
    if (i < 0 || j < 0) return selectOnly(path);
    const [lo, hi] = i <= j ? [i, j] : [j, i];
    setSelected(new Set(visiblePaths.slice(lo, hi + 1)));
  }

  function onRowClick(path: string, e: MouseEvent) {
    const meta = e.metaKey || e.ctrlKey;
    if (e.shiftKey) {
      selectRange(path);
    } else if (meta) {
      selectToggle(path);
    } else {
      selectOnly(path);
    }
  }

  function onBodyClick(e: MouseEvent) {
    // Click on empty space inside the body clears selection.
    if ((e.target as HTMLElement).classList.contains("artifacts-body")) {
      setSelected(new Set());
      setAnchor(null);
    }
  }

  // ─── Keyboard ────────────────────────────────────────────────────────────

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (renaming) return;            // rename input handles its own keys
      if (menu) return;                // menu handles Escape itself

      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }

      if (selected.size === 0) return;

      const meta = e.metaKey || e.ctrlKey;
      if ((e.key === "Backspace" && meta) || e.key === "Delete") {
        e.preventDefault();
        deleteSelection();
        return;
      }

      if (e.key === "Enter" && selected.size === 1) {
        e.preventDefault();
        const only = [...selected][0];
        if (isRenameable(only)) setRenaming(only);
        return;
      }

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!anchor) return;
        const i = visiblePaths.indexOf(anchor);
        if (i < 0) return;
        const next = e.key === "ArrowDown"
          ? Math.min(visiblePaths.length - 1, i + 1)
          : Math.max(0, i - 1);
        const nextPath = visiblePaths[next];
        if (e.shiftKey) selectRange(nextPath);
        else selectOnly(nextPath);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selected, anchor, visiblePaths, renaming, menu, onClose]);

  // ─── Context menu ────────────────────────────────────────────────────────

  function onContextMenu(path: string, e: MouseEvent) {
    // If clicked row isn't in the current selection, replace it.
    let nextSel = selected;
    if (!selected.has(path)) {
      nextSel = new Set([path]);
      setSelected(nextSel);
      setAnchor(path);
    }
    const items = buildMenu(nextSel);
    log.info("artifacts", "context menu open", {
      path,
      selection: [...nextSel],
      items: items.map((it: any) => ({ label: it.label, disabled: !!it.disabled })),
    });
    setMenu({ x: e.clientX, y: e.clientY, items });
  }

  function buildMenu(sel: Set<string>): MenuItem[] {
    const paths = [...sel];
    const single = paths.length === 1 ? paths[0] : null;
    const singleNode = single && data ? findNode(data.roots, single) : null;
    const allDeletable = paths.length > 0 && paths.every(isDeletable);
    const singleRenameable = single ? isRenameable(single) : false;
    const singleRunnable = single ? isRunnable(single) : false;
    const singleIsFile = singleNode?.kind === "file";
    const singleIsSnapshot = single ? classifyPath(single) === "snapshot" : false;

    return [
      {
        label: "Open",
        disabled: !singleIsFile,
        onClick: () => single && singleNode && openFile(single, singleNode),
      },
      {
        label: "Run",
        shortcut: "⌘R",
        disabled: !singleRunnable,
        onClick: () => single && runReport(single),
      },
      { label: "---" } as MenuItem,
      {
        label: "Reveal handle",
        disabled: !singleIsSnapshot,
        onClick: () => single && singleNode && revealHandle(singleNode),
      },
      {
        label: "Rename",
        disabled: !singleRenameable,
        onClick: () => single && setRenaming(single),
      },
      { label: "---" } as MenuItem,
      {
        label: paths.length > 1 ? `Delete ${paths.length} items` : "Delete",
        shortcut: "⌘⌫",
        disabled: !allDeletable,
        // Pass paths explicitly — ``deleteSelection`` is a closure over
        // ``selected`` from THIS render. When the user right-clicks, we
        // call setSelected(nextSel) but the new state hasn't propagated
        // yet, so the closure still sees the previous ``selected``. The
        // menu items captured at right-click time would then read a
        // stale (often empty) set. Hand the captured ``paths`` over
        // directly to dodge that whole class of bug.
        onClick: () => deleteSelection(paths),
      },
    ];
  }

  // ─── Actions ─────────────────────────────────────────────────────────────

  function openFile(path: string, node: ArtifactNode) {
    // Map the rel-path to a backend file URL. Two shapes exist:
    //   python_storage/cache/snapshot_<handle>/<filename>
    //   python_storage/compute/<slug>/runs/<run_id>/<filename>
    // Anything else (script source files like code.py / meta.json) is
    // intentionally not openable — there's no file-serving route for them.
    const psMatch = /^python_storage\/cache\/snapshot_([A-Za-z0-9_-]+)\/(.+)$/.exec(path);
    if (psMatch) {
      const [, handle, fname] = psMatch;
      window.open(`${backendOrigin}/api/python-storage/${handle}/${encodeURIComponent(fname)}`, "_blank");
      return;
    }
    const runMatch = /^python_storage\/compute\/([a-z0-9_-]+)\/runs\/([A-Za-z0-9_-]+)\/(.+)$/.exec(path);
    if (runMatch) {
      const [, slug, runId, fname] = runMatch;
      window.open(`${backendOrigin}/api/script-output/${slug}/${runId}/${encodeURIComponent(fname)}`, "_blank");
      return;
    }
    log.warn("artifacts", "no open path for file", { path, kind: node.kind });
  }

  async function revealHandle(node: ArtifactNode) {
    const h = node.snapshot?.handle;
    if (!h) return;
    try {
      await navigator.clipboard.writeText(h);
      log.info("artifacts", "handle copied", { handle: h });
    } catch (e: any) {
      log.warn("artifacts", "clipboard write failed", { message: e?.message });
    }
  }

  async function runReport(path: string) {
    log.info("artifacts", "run start", { path });
    try {
      const resp = await fetch(`${backendOrigin}/api/artifacts/${encodeURI(path)}/run`, {
        method: "POST",
        credentials: "include",
      });
      if (!resp.ok) {
        const detail = await resp.text();
        throw new Error(`run failed: ${resp.status} ${detail}`);
      }
      const body: any = await resp.json();
      if (body.kind === "holoviz") {
        await invokePrimitive("show_report", {
          path: body.path,
          report_id: body.report_id,
          title: body.title,
        });
      } else if (body.kind === "flow") {
        await invokePrimitive("show_flow_report", {
          definition: body.definition,
          report_id: body.report_id,
          title: body.title,
          render_id: body.render_id,
        });
      } else {
        throw new Error(`unknown run kind: ${body.kind}`);
      }
    } catch (e: any) {
      log.error("artifacts", "run failed", { path, message: e?.message });
      alert(`Run failed: ${e?.message || e}`);
    }
  }

  async function renameCommit(path: string, next: string) {
    log.info("artifacts", "rename start", { path, next });
    setRenaming(null);
    const kind = classifyPath(path);
    if (!kind || kind === "run") return;
    const body: Record<string, string> =
      kind === "snapshot" ? { display_name: next } : { slug: next };
    let newPath: string | null = null;
    try {
      const resp = await fetch(`${backendOrigin}/api/artifacts/${encodeURI(path)}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const detail = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      const respBody = await resp.json();
      // Slug renames return the new path; snapshot rename keeps the
      // same dir name (handle is canonical), so the row identity is
      // unchanged and ``newPath`` stays the original.
      newPath = (typeof respBody?.path === "string" ? respBody.path : null) || path;
    } catch (e: any) {
      log.error("artifacts", "rename failed", { path, message: e?.message });
      alert(`Rename failed: ${e?.message || e}`);
    }
    // Migrate expanded / selected state from the old path to the new
    // one before the refetch lands, so the user's view doesn't lose
    // its place. Path rewriting is prefix-based: renaming
    // ``scripts/flows/foo`` → ``scripts/flows/bar`` also moves
    // ``scripts/flows/foo/anything`` (none today, but future-safe).
    if (newPath && newPath !== path) {
      const remap = (s: Set<string>): Set<string> => {
        const out = new Set<string>();
        for (const p of s) {
          if (p === path) out.add(newPath!);
          else if (p.startsWith(path + "/")) out.add(newPath! + p.slice(path.length));
          else out.add(p);
        }
        return out;
      };
      setExpanded((prev) => remap(prev));
      setSelected((prev) => remap(prev));
      setAnchor((a) => (a === path ? newPath : a));
    }
    await fetchArtifacts();
  }

  async function deleteSelection(pathsArg?: string[]) {
    // ``pathsArg`` lets the context-menu item pass the captured selection
    // from right-click time, avoiding the stale-closure trap. Keyboard
    // shortcuts (Cmd+Backspace / Delete) leave it undefined and we fall
    // back to current state — which is fine because keyboard fires off
    // the latest render's closure.
    const paths = (pathsArg ?? [...selected]).filter(isDeletable);
    log.info("artifacts", "delete start", { paths });
    if (paths.length === 0) return;
    const label = paths.length === 1 ? "this item" : `${paths.length} items`;
    if (!confirm(`Delete ${label}? This cannot be undone.`)) return;
    const failures: string[] = [];
    for (const p of paths) {
      try {
        const resp = await fetch(`${backendOrigin}/api/artifacts/${encodeURI(p)}`, {
          method: "DELETE",
          credentials: "include",
        });
        if (!resp.ok) {
          const detail = await resp.text();
          failures.push(`${p}: ${resp.status} ${detail}`);
        }
      } catch (e: any) {
        failures.push(`${p}: ${e?.message || e}`);
      }
    }
    if (failures.length) {
      log.error("artifacts", "delete failures", { failures });
      alert(`Failed to delete:\n${failures.join("\n")}`);
    }
    await fetchArtifacts();
  }

  // ─── Render ──────────────────────────────────────────────────────────────

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
            try { localStorage.removeItem(STORAGE_RECT_KEY); } catch { /* ignore */ }
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
          <button type="button" class="art-btn" onClick={expandAll} title="Expand all">Expand all</button>
          <button type="button" class="art-btn" onClick={collapseAll} title="Collapse all">Collapse all</button>
          <button type="button" class="art-btn" onClick={() => fetchArtifacts(true)} title="Refresh">↻</button>
          <button
            type="button"
            class="art-btn art-btn-close"
            onClick={onClose}
            title="Close"
            aria-label="Close"
          >×</button>
        </div>
        <div class="artifacts-columns">
          <span class="art-col-name">Name</span>
          <span class="art-col-size">Size</span>
          <span class="art-col-mtime">Modified</span>
        </div>
        <div class="artifacts-body" onClick={onBodyClick}>
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
                  selected={selected}
                  renaming={renaming}
                  collisionNames={new Set()}
                  onToggle={toggle}
                  onClick={onRowClick}
                  onContextMenu={onContextMenu}
                  onRenameCommit={renameCommit}
                  onRenameCancel={() => setRenaming(null)}
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
              {selected.size > 0 && ` · ${selected.size} selected`}
            </span>
          )}
        </div>
        {menu && <ContextMenu state={menu} onDismiss={() => setMenu(null)} />}
      </div>
    </div>
  );
}
