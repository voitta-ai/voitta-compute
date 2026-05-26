// Floating draggable workspace browser panel.
// Shows scripts and data snapshots grouped by folder, with run/delete/preview/move actions.

import { useCallback, useEffect, useRef, useState } from "react";
import { useSetRecoilState } from "recoil";
import { activeTabState, reportCollapsedState, reportLoadingState, reportsState } from "../report/state";

// ─── types ────────────────────────────────────────────────────────────────────

interface ScriptItem {
  kind: "script";
  id: string;
  slug: string;
  title: string;
  last_run_at: string | null;
  last_ok: boolean | null;
  last_kind: string | null;
  folder_name: string | null;
}

interface DataFile {
  name: string;
  bytes: number;
  variant?: string | null;
}

interface DataItem {
  kind: "data";
  id: string;
  handle: string;
  name: string;
  bytes: number;
  file_count: number;
  files: DataFile[];
  created_at: string | null;
  source: string | null;
  asset: string | null;
  data_kind: string | null;
  corrupt: boolean;
  folder_name: string | null;
}

interface FolderMeta {
  name: string;
  description: string;
  color: string;
  created_at: string | null;
  data_count: number;
  script_count: number;
}

interface PreviewState {
  handle: string;
  filename: string;
  url: string;
}

// ─── credentialed image ───────────────────────────────────────────────────────
// Plain <img src="https://127.0.0.1:..."> fails on self-signed TLS when
// loaded from a public host page. Fetch with credentials to get a blob URL.
function CredentialedImg({ src, alt, className, onClick }: {
  src: string; alt: string; className?: string; onClick?: () => void;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  useEffect(() => {
    let url = "";
    fetch(src, { credentials: "include" })
      .then(r => r.blob())
      .then(b => { url = URL.createObjectURL(b); setBlobUrl(url); })
      .catch(() => {});
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [src]);
  if (!blobUrl) return null;
  return <img src={blobUrl} alt={alt} className={className} onClick={onClick} />;
}

// ─── file type helpers ────────────────────────────────────────────────────────
const IMG_EXTS  = new Set(["jpg","jpeg","png","gif","webp","svg","bmp","ico","avif"]);
const VID_EXTS  = new Set(["mp4","webm","mov","mkv","m4v"]);
const AUD_EXTS  = new Set(["mp3","wav","ogg","m4a","flac","aac"]);
const TEXT_EXTS = new Set([
  "txt","md","csv","json","yaml","yml","log","py","js","ts","jsx","tsx",
  "html","css","xml","sh","toml","ini","cfg","sql","r","ipynb",
]);

function fileExt(name: string) { return (name.split(".").pop() ?? "").toLowerCase(); }
function isImage(name: string) { return IMG_EXTS.has(fileExt(name)); }
function isVideo(name: string) { return VID_EXTS.has(fileExt(name)); }
function isAudio(name: string) { return AUD_EXTS.has(fileExt(name)); }
function isText(name: string)  { return TEXT_EXTS.has(fileExt(name)); }
function canPreview(name: string) {
  return isImage(name) || isVideo(name) || isAudio(name) || isText(name);
}

// ─── helpers ──────────────────────────────────────────────────────────────────

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtDate(s: string | null): string {
  if (!s) return "";
  try { return new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
  catch { return ""; }
}

function assetLabel(asset: string | null, dataKind: string | null): { text: string; cls: string } | null {
  if (asset === "cad_mesh")        return { text: "CAD mesh",    cls: "ws-badge-cad" };
  if (asset === "cad_projection")  return { text: "CAD views",   cls: "ws-badge-cad" };
  if (asset === "md")              return { text: "Markdown",    cls: "ws-badge-md" };
  if (asset === "original")        return { text: "File",        cls: "ws-badge-file" };
  if (dataKind === "drive_file")   return { text: "Drive",       cls: "ws-badge-drive" };
  if (dataKind === "fetched_url")  return { text: "Web fetch",   cls: "ws-badge-web" };
  if (dataKind === "veed_frame")   return { text: "VEED frame",  cls: "ws-badge-veed" };
  if (asset)                       return { text: asset,         cls: "ws-badge-file" };
  return null;
}

function sourceLabel(source: string | null): string | null {
  if (!source) return null;
  const map: Record<string, string> = { vre: "VRE", drive: "Drive", web: "Web", video_seek: "VEED" };
  return map[source] ?? source;
}

// ─── icons ────────────────────────────────────────────────────────────────────

function ScriptIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" className="ws-icon ws-icon-script">
      <rect x="2" y="2" width="16" height="16" rx="3" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <text x="4" y="14" fontFamily="ui-monospace,monospace" fontSize="9" fill="currentColor">&gt;_</text>
    </svg>
  );
}

function DataIcon({ corrupt }: { corrupt?: boolean }) {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" className={`ws-icon ws-icon-data${corrupt ? " ws-icon-corrupt" : ""}`}>
      <ellipse cx="10" cy="6" rx="7" ry="3" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3 6v4c0 1.66 3.13 3 7 3s7-1.34 7-3V6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3 10v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function FolderIcon({ open }: { open?: boolean }) {
  return (
    <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true" className="ws-icon ws-icon-folder">
      {open
        ? <path d="M2 6h5l2 2h9v8H2V6z M2 10h16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
        : <path d="M2 6h5l2 2h9v8H2V6z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />}
    </svg>
  );
}

function FileIcon({ variant }: { variant?: string | null }) {
  if (variant === "front" || variant === "top" || variant === "side" || variant === "iso") {
    return (
      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" className="ws-file-icon ws-file-icon-img">
        <rect x="2" y="2" width="12" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <circle cx="5.5" cy="5.5" r="1.2" fill="currentColor" opacity="0.7" />
        <path d="M2 10l3.5-3 2.5 2.5 2-1.5 4 4" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" className="ws-file-icon">
      <path d="M4 2h6l3 3v9H4V2z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M10 2v3h3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}

function EyeIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
      <path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M8 2v8M5 7l3 4 3-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M3 13h10" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function StatusDot({ ok }: { ok: boolean | null }) {
  if (ok === null) return null;
  return <span className={`ws-status-dot ${ok ? "ok" : "err"}`} title={ok ? "Last run succeeded" : "Last run failed"} />;
}

// ─── props ────────────────────────────────────────────────────────────────────

interface Props {
  backendOrigin: string;
  embedded?: boolean;
  onClose?: () => void;
  onOpenReport?: () => void;
}

// ─── component ────────────────────────────────────────────────────────────────

export default function WorkspacePanel({ backendOrigin, embedded, onClose, onOpenReport }: Props) {
  const [scripts, setScripts] = useState<ScriptItem[]>([]);
  const [data, setData] = useState<DataItem[]>([]);
  const [folders, setFolders] = useState<FolderMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [folderExpanded, setFolderExpanded] = useState<Set<string>>(new Set());

  // new folder form
  const [newFolderName, setNewFolderName] = useState("");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [showNewFolder, setShowNewFolder] = useState(false);

  // move dropdown
  const [moveTarget, setMoveTarget] = useState<{ id: string; kind: "data" | "script" } | null>(null);

  // preview
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewTextLoading, setPreviewTextLoading] = useState(false);

  const setReports = useSetRecoilState(reportsState);
  const setActiveTab = useSetRecoilState(activeTabState);
  const setReportCollapsed = useSetRecoilState(reportCollapsedState);
  const setReportLoading = useSetRecoilState(reportLoadingState);

  const [pos, setPos] = useState({ x: 60, y: 60 });
  const posRef = useRef(pos);
  posRef.current = pos;

  // ─── drag ─────────────────────────────────────────────────────────────────
  const onHeaderDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    const start = { mx: e.clientX, my: e.clientY, px: posRef.current.x, py: posRef.current.y };
    const move = (ev: PointerEvent) => setPos({ x: start.px + ev.clientX - start.mx, y: start.py + ev.clientY - start.my });
    const up = (ev: PointerEvent) => {
      try { target.releasePointerCapture(ev.pointerId); } catch { }
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  }, []);

  // ─── fetch ────────────────────────────────────────────────────────────────
  const fetchWorkspace = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`${backendOrigin}/api/workspace`, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setScripts(d.scripts ?? []);
      setData(d.data ?? []);
      setFolders(d.folders ?? []);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [backendOrigin]);

  useEffect(() => { fetchWorkspace(); }, [fetchWorkspace]);

  // ─── preview ──────────────────────────────────────────────────────────────
  const fileUrl = useCallback((handle: string, filename: string) =>
    `${backendOrigin}/api/workspace/data/${handle}/files/${encodeURIComponent(filename)}`,
    [backendOrigin]);

  const openPreview = useCallback((handle: string, filename: string) => {
    setPreview({ handle, filename, url: fileUrl(handle, filename) });
    setPreviewText(null);
  }, [fileUrl]);

  const closePreview = useCallback(() => { setPreview(null); setPreviewText(null); }, []);

  useEffect(() => {
    if (!preview || !isText(preview.filename)) return;
    setPreviewTextLoading(true);
    fetch(preview.url, { credentials: "include" })
      .then(r => r.text())
      .then(t => setPreviewText(t.length > 100_000 ? t.slice(0, 100_000) + "\n[truncated]" : t))
      .catch(e => setPreviewText(`Error: ${e}`))
      .finally(() => setPreviewTextLoading(false));
  }, [preview]);

  useEffect(() => {
    if (!preview) return;
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") closePreview(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [preview, closePreview]);

  // ─── folder CRUD ──────────────────────────────────────────────────────────
  const createFolder = useCallback(async (name: string) => {
    if (!name.trim()) return;
    setCreatingFolder(true);
    try {
      await fetch(`${backendOrigin}/api/workspace/folders`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      setNewFolderName("");
      setShowNewFolder(false);
      await fetchWorkspace();
    } finally { setCreatingFolder(false); }
  }, [backendOrigin, fetchWorkspace]);

  const deleteFolder = useCallback(async (name: string) => {
    await fetch(`${backendOrigin}/api/workspace/folders/${encodeURIComponent(name)}`, {
      method: "DELETE", credentials: "include",
    });
    await fetchWorkspace();
  }, [backendOrigin, fetchWorkspace]);

  const moveItem = useCallback(async (id: string, kind: "data" | "script", folderName: string | null) => {
    const url = kind === "data"
      ? `${backendOrigin}/api/workspace/data/${id}`
      : `${backendOrigin}/api/workspace/scripts/${id}`;
    await fetch(url, {
      method: "PATCH", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: folderName }),
    });
    setMoveTarget(null);
    await fetchWorkspace();
  }, [backendOrigin, fetchWorkspace]);

  // ─── script actions ───────────────────────────────────────────────────────
  const _runScript = useCallback(async (slug: string, target: "app" | "tab") => {
    if (target === "app") setReportLoading(true);
    setRunning((s) => new Set(s).add(`${slug}:${target}`));
    try {
      const r = await fetch(`${backendOrigin}/api/workspace/scripts/${slug}/run`, {
        method: "POST", credentials: "include",
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) { setReportLoading(false); return; }
      if (d.url) {
        if (target === "app") {
          const entry = {
            name: slug, title: slug.replace(/-/g, " "),
            render_id: d.render_id ?? slug,
            payload: { kind: "html" as const, url: d.url },
          };
          setReports((prev) => {
            const byName = prev.findIndex((r) => r.name === entry.name);
            const byId   = prev.findIndex((r) => r.render_id === entry.render_id);
            const idx = byName >= 0 ? byName : byId;
            return idx >= 0 ? prev.map((r, i) => (i === idx ? entry : r)) : [...prev, entry];
          });
          setActiveTab(entry.render_id);
          setReportCollapsed(false);
          onClose?.();
        } else {
          window.open(`${backendOrigin}${d.url}`, "_blank");
        }
      }
      await fetchWorkspace();
    } finally {
      setRunning((s) => { const n = new Set(s); n.delete(`${slug}:${target}`); return n; });
    }
  }, [backendOrigin, fetchWorkspace, onClose, onOpenReport, setReports, setActiveTab, setReportCollapsed, setReportLoading]);

  const deleteScript = useCallback(async (slug: string) => {
    setDeleting((s) => new Set(s).add(slug));
    try {
      await fetch(`${backendOrigin}/api/workspace/scripts/${slug}`, { method: "DELETE", credentials: "include" });
      setScripts((arr) => arr.filter((x) => x.slug !== slug));
    } finally { setDeleting((s) => { const n = new Set(s); n.delete(slug); return n; }); }
  }, [backendOrigin]);

  const deleteData = useCallback(async (handle: string) => {
    setDeleting((s) => new Set(s).add(handle));
    try {
      await fetch(`${backendOrigin}/api/workspace/data/${handle}`, { method: "DELETE", credentials: "include" });
      setData((arr) => arr.filter((x) => x.handle !== handle));
    } finally { setDeleting((s) => { const n = new Set(s); n.delete(handle); return n; }); }
  }, [backendOrigin]);

  // ─── move dropdown ────────────────────────────────────────────────────────
  function renderMoveDropdown(id: string, kind: "data" | "script", currentFolder: string | null) {
    if (moveTarget?.id !== id) return null;
    const opts = folders.map(f => f.name).filter(n => n !== currentFolder);
    return (
      <div className="ws-move-dropdown" onClick={e => e.stopPropagation()}>
        {currentFolder && (
          <button className="ws-move-opt ws-move-opt-root" onClick={() => moveItem(id, kind, null)}>
            ↑ Remove from folder
          </button>
        )}
        {opts.map(n => (
          <button key={n} className="ws-move-opt" onClick={() => moveItem(id, kind, n)}>
            → {n}
          </button>
        ))}
        {opts.length === 0 && !currentFolder && (
          <span className="ws-move-empty">No folders yet</span>
        )}
        <button className="ws-move-cancel" onClick={() => setMoveTarget(null)}>Cancel</button>
      </div>
    );
  }

  // ─── row renderers ────────────────────────────────────────────────────────
  function renderScriptRow(s: ScriptItem) {
    return (
      <li key={s.slug} className="ws-row">
        <ScriptIcon />
        <StatusDot ok={s.last_ok} />
        <span className="ws-row-name" title={s.title}>{s.title}</span>
        <span className="ws-row-meta">{fmtDate(s.last_run_at)}</span>
        {folders.length > 0 && (
          <div className="ws-move-wrap">
            <button className="ws-act ws-act-move" type="button" title="Move to folder"
              onClick={() => setMoveTarget(moveTarget?.id === s.slug ? null : { id: s.slug, kind: "script" })}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <path d="M1 4h5l1.5 1.5H9v1H6l-1.5-1.5H2v8h12V7h1v6H1V4z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
                <path d="M12 2v6M9 5l3-3 3 3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
            {renderMoveDropdown(s.slug, "script", s.folder_name)}
          </div>
        )}
        <button className="ws-act ws-act-run" type="button" title="Run — show in app"
          disabled={running.has(`${s.slug}:app`) || running.has(`${s.slug}:tab`)}
          onClick={() => _runScript(s.slug, "app")}>
          {running.has(`${s.slug}:app`)
            ? <svg viewBox="0 0 16 16" width="11" height="11"><circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="16 8"/></svg>
            : <svg viewBox="0 0 16 16" width="11" height="11"><polygon points="3,3 12,8 3,13" fill="currentColor"/></svg>}
        </button>
        <button className="ws-act ws-act-run-tab" type="button" title="Run — open in new tab"
          disabled={running.has(`${s.slug}:app`) || running.has(`${s.slug}:tab`)}
          onClick={() => _runScript(s.slug, "tab")}>
          {running.has(`${s.slug}:tab`)
            ? <svg viewBox="0 0 16 16" width="11" height="11"><circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="16 8"/></svg>
            : <svg viewBox="0 0 16 16" width="11" height="11"><path d="M9 3h4v4M13 3l-6 6M6 4H4a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg>}
        </button>
        <button className="ws-act ws-act-del" type="button" title="Delete script"
          disabled={deleting.has(s.slug)} onClick={() => deleteScript(s.slug)}>
          <svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 4h10M6 4V2h4v2M5 4v9h6V4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </button>
      </li>
    );
  }

  function renderDataRow(d: DataItem) {
    const isExpanded = expanded.has(d.handle);
    const canExpand = (d.files?.length ?? 0) > 0;
    const badge = assetLabel(d.asset, d.data_kind);
    const src = sourceLabel(d.source);
    return (
      <li key={d.handle} className={`ws-row-group${d.corrupt ? " ws-row-corrupt" : ""}`}>
        <div className="ws-row">
          {canExpand ? (
            <button className="ws-expand-btn" type="button" title={isExpanded ? "Collapse" : "Expand"} aria-expanded={isExpanded}
              onClick={() => setExpanded(s => { const n = new Set(s); isExpanded ? n.delete(d.handle) : n.add(d.handle); return n; })}>
              <svg viewBox="0 0 10 10" width="9" height="9" style={{ transform: isExpanded ? "rotate(90deg)" : undefined, transition: "transform 0.15s" }}>
                <path d="M2 2l5 3-5 3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          ) : <span className="ws-expand-spacer" />}
          <DataIcon corrupt={d.corrupt} />
          {src && <span className="ws-badge ws-badge-src" title={`Source: ${src}`}>{src}</span>}
          {badge && <span className={`ws-badge ${badge.cls}`} title={`Asset type: ${badge.text}`}>{badge.text}</span>}
          <span className="ws-row-name" title={d.name}>{d.name}</span>
          <span className="ws-row-meta">{fmtBytes(d.bytes)}</span>
          <span className="ws-row-meta ws-row-date">{fmtDate(d.created_at)}</span>
          {folders.length > 0 && (
            <div className="ws-move-wrap">
              <button className="ws-act ws-act-move" type="button" title="Move to folder"
                onClick={() => setMoveTarget(moveTarget?.id === d.handle ? null : { id: d.handle, kind: "data" })}>
                <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                  <path d="M1 4h5l1.5 1.5H9v1H6l-1.5-1.5H2v8h12V7h1v6H1V4z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
                  <path d="M12 2v6M9 5l3-3 3 3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
              {renderMoveDropdown(d.handle, "data", d.folder_name)}
            </div>
          )}
          <button className="ws-act ws-act-del" type="button" title="Delete snapshot"
            aria-label={`Delete ${d.name}`} disabled={deleting.has(d.handle)}
            onClick={() => deleteData(d.handle)}>
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 4h10M6 4V2h4v2M5 4v9h6V4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>
        {isExpanded && canExpand && (
          <ul className="ws-file-list">
            {d.files.map(f => (
              <li key={f.name} className="ws-file-row">
                {isImage(f.name)
                  ? <CredentialedImg src={fileUrl(d.handle, f.name)} alt="" className="ws-file-thumb" onClick={() => openPreview(d.handle, f.name)} />
                  : <FileIcon variant={f.variant} />}
                {f.variant && <span className="ws-file-variant">{f.variant}</span>}
                <span className="ws-file-name" title={f.name}>{f.name}</span>
                <span className="ws-file-size">{fmtBytes(f.bytes)}</span>
                {canPreview(f.name) ? (
                  <button className="ws-act ws-act-preview" type="button" title="Preview" onClick={() => openPreview(d.handle, f.name)}>
                    <EyeIcon />
                  </button>
                ) : (
                  <a href={fileUrl(d.handle, f.name)} download={f.name} className="ws-act ws-act-dl" title="Download"><DownloadIcon /></a>
                )}
              </li>
            ))}
          </ul>
        )}
      </li>
    );
  }

  // ─── folder section ───────────────────────────────────────────────────────
  function renderFolder(folder: FolderMeta) {
    const isOpen = folderExpanded.has(folder.name);
    const folderScripts = scripts.filter(s => s.folder_name === folder.name);
    const folderData = data.filter(d => d.folder_name === folder.name);
    const total = folderScripts.length + folderData.length;
    return (
      <div key={folder.name} className="ws-folder-group">
        <div className="ws-folder-header">
          <button className="ws-folder-toggle" type="button"
            onClick={() => setFolderExpanded(s => { const n = new Set(s); isOpen ? n.delete(folder.name) : n.add(folder.name); return n; })}>
            <svg viewBox="0 0 10 10" width="9" height="9" style={{ transform: isOpen ? "rotate(90deg)" : undefined, transition: "transform 0.15s" }}>
              <path d="M2 2l5 3-5 3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <FolderIcon open={isOpen} />
            <span className="ws-folder-name">{folder.name}</span>
            {folder.description && <span className="ws-folder-desc">{folder.description}</span>}
            <span className="ws-folder-count">{total}</span>
          </button>
          <button className="ws-act ws-act-del ws-folder-del" type="button" title="Delete folder (items moved to root)"
            onClick={() => deleteFolder(folder.name)}>
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 4h10M6 4V2h4v2M5 4v9h6V4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>
        {isOpen && (
          <div className="ws-folder-body">
            {folderScripts.length > 0 && (
              <ul className="ws-list">{folderScripts.map(renderScriptRow)}</ul>
            )}
            {folderData.length > 0 && (
              <ul className="ws-list">{folderData.map(renderDataRow)}</ul>
            )}
            {total === 0 && <div className="ws-folder-empty">Empty folder</div>}
          </div>
        )}
      </div>
    );
  }

  // ─── preview modal ────────────────────────────────────────────────────────
  function renderPreviewContent(p: PreviewState) {
    const { filename, url } = p;
    if (isImage(filename)) return <CredentialedImg src={url} alt={filename} className="ws-preview-img" />;
    if (isVideo(filename)) return <video src={url} controls className="ws-preview-video" />;
    if (isAudio(filename)) return <audio src={url} controls className="ws-preview-audio" />;
    if (isText(filename)) {
      if (previewTextLoading) return <div className="ws-preview-loading">Loading…</div>;
      return <pre className="ws-preview-text">{previewText ?? ""}</pre>;
    }
    return (
      <div className="ws-preview-download">
        <a href={url} download={filename} className="ws-preview-dl-btn"><DownloadIcon /> Download {filename}</a>
      </div>
    );
  }

  const previewModal = preview ? (
    <div className="ws-preview-backdrop" onClick={closePreview} role="dialog" aria-modal="true">
      <div className="ws-preview-modal" onClick={e => e.stopPropagation()}>
        <div className="ws-preview-header">
          <span className="ws-preview-title" title={preview.filename}>{preview.filename}</span>
          <a href={preview.url} download={preview.filename} className="ws-hbtn" title="Download" onClick={e => e.stopPropagation()}><DownloadIcon /></a>
          <button className="ws-hbtn" type="button" title="Close (Esc)" onClick={closePreview}>×</button>
        </div>
        <div className="ws-preview-body">{renderPreviewContent(preview)}</div>
      </div>
    </div>
  ) : null;

  // ─── body ─────────────────────────────────────────────────────────────────
  const unfolderedScripts = scripts.filter(s => !s.folder_name);
  const unfolderedData    = data.filter(d => !d.folder_name);
  const isEmpty = scripts.length === 0 && data.length === 0;

  function renderBody() {
    return (
      <>
        {loading && <div className="ws-empty">Loading…</div>}
        {error && <div className="ws-empty ws-error">{error}</div>}
        {!loading && !error && isEmpty && <div className="ws-empty">No scripts or data snapshots yet.</div>}

        {/* New folder form */}
        {!loading && showNewFolder && (
          <div className="ws-new-folder-form">
            <input
              className="ws-folder-input"
              type="text"
              placeholder="folder-name"
              value={newFolderName}
              onChange={e => setNewFolderName(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))}
              onKeyDown={e => { if (e.key === "Enter") createFolder(newFolderName); if (e.key === "Escape") setShowNewFolder(false); }}
              autoFocus
              maxLength={64}
            />
            <button className="ws-act ws-act-run" type="button" disabled={creatingFolder || !newFolderName}
              onClick={() => createFolder(newFolderName)}>✓</button>
            <button className="ws-act" type="button" onClick={() => setShowNewFolder(false)}>✕</button>
          </div>
        )}

        {/* Folders */}
        {!loading && folders.length > 0 && (
          <section className="ws-section">
            <div className="ws-section-label">Folders</div>
            {folders.map(renderFolder)}
          </section>
        )}

        {/* Unfoldered scripts */}
        {!loading && unfolderedScripts.length > 0 && (
          <section className="ws-section">
            <div className="ws-section-label">Scripts</div>
            <ul className="ws-list">{unfolderedScripts.map(renderScriptRow)}</ul>
          </section>
        )}

        {/* Unfoldered data */}
        {!loading && unfolderedData.length > 0 && (
          <section className="ws-section">
            <div className="ws-section-label">Data</div>
            <ul className="ws-list">{unfolderedData.map(renderDataRow)}</ul>
          </section>
        )}
      </>
    );
  }

  const refreshBtn = (
    <button className={embedded ? "hbtn" : "ws-hbtn"} type="button" title="Refresh" onClick={fetchWorkspace}>
      <svg viewBox="0 0 20 20" width="13" height="13"><path d="M16 10a6 6 0 1 1-1.5-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/><path d="M14.5 6V3.5H17" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>
    </button>
  );

  const newFolderBtn = (
    <button className={embedded ? "hbtn" : "ws-hbtn"} type="button" title="New folder"
      onClick={() => { setShowNewFolder(v => !v); setNewFolderName(""); }}>
      <svg viewBox="0 0 20 20" width="13" height="13"><path d="M2 5h5l2 2h9v9H2V5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><path d="M10 10v4M8 12h4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
    </button>
  );

  if (embedded) {
    return (
      <>
        <div className="ws-embedded">
          <div className="ws-embedded-toolbar">{newFolderBtn}{refreshBtn}</div>
          <div className="ws-body ws-embedded-body">{renderBody()}</div>
        </div>
        {previewModal}
      </>
    );
  }

  return (
    <>
      <div className="ws-panel" style={{ left: pos.x, top: pos.y }} role="dialog" aria-label="Workspace">
        <div className="ws-header" onPointerDown={onHeaderDown}>
          <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" className="ws-header-icon">
            <path d="M2 5h6l2 2h8v10H2V5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
          </svg>
          <span className="ws-title">Workspace</span>
          <span className="ws-spacer" />
          {newFolderBtn}
          {refreshBtn}
          {onClose && <button className="ws-hbtn" type="button" title="Close" onClick={onClose}>×</button>}
        </div>
        <div className="ws-body">{renderBody()}</div>
      </div>
      {previewModal}
    </>
  );
}
