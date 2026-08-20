// Project chip — chat-header control showing the ACTIVE project's name,
// with a dropdown to switch, create (inline, one field), rename and
// delete projects. Switching a project re-keys the chat pane (fresh
// conversation in the new project) via onSwitched — same gesture as
// "+ new conversation".
//
// Minimal-friction rules: the chip always shows where you are; creating
// a project is one text field; there is no modal. Legacy is undeletable
// (backend-enforced; the ✕ is simply not rendered for it).

import { useCallback, useEffect, useRef, useState } from "react";

interface ProjectInfo {
  slug: string;
  name: string;
  created_at: string;
  is_legacy: boolean;
}

interface ProjectsResponse {
  ok: boolean;
  active: string;
  projects: ProjectInfo[];
}

interface Props {
  backendOrigin: string;
  onSwitched: () => void;
}

export default function ProjectChip({ backendOrigin, onSwitched }: Props) {
  const [data, setData] = useState<ProjectsResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${backendOrigin}/api/projects`, { credentials: "include" });
      if (!r.ok) throw new Error(`status ${r.status}`);
      setData((await r.json()) as ProjectsResponse);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [backendOrigin]);

  useEffect(() => { void refresh(); }, [refresh]);

  // Close on outside click / focus loss / mouse-out. Three layers because
  // the widget lives among iframes: document mousedown misses clicks that
  // land inside the chat iframe or on the host page, window blur catches
  // those (clicking an iframe blurs this window), and mouseleave (with a
  // short grace delay so skimming the edge doesn't dismiss) covers plain
  // mouse-away.
  const closeTimer = useRef<number | null>(null);
  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 300);
  }, [cancelClose]);
  useEffect(() => {
    if (!open) { cancelClose(); return; }
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onBlur = () => setOpen(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("blur", onBlur);
      cancelClose();
    };
  }, [open, cancelClose]);

  async function post(path: string, body?: unknown, method = "POST") {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`${backendOrigin}${path}`, {
        method,
        credentials: "include",
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail.slice(0, 160) || `status ${r.status}`);
      }
      setData((await r.json()) as ProjectsResponse);
      return true;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function switchTo(slug: string) {
    if (slug === data?.active) { setOpen(false); return; }
    if (await post("/api/projects/active", { slug })) {
      setOpen(false);
      onSwitched();
    }
  }

  async function createProject() {
    const name = newName.trim();
    if (!name) return;
    if (await post("/api/projects", { name })) {
      // Created — now switch into it (create intentionally doesn't switch
      // server-side; the chip's gesture is create-and-go).
      const fresh = await fetch(`${backendOrigin}/api/projects`, { credentials: "include" })
        .then((r) => r.json() as Promise<ProjectsResponse>)
        .catch(() => null);
      const created = fresh?.projects.find(
        (p) => p.name === name && !p.is_legacy,
      );
      setNewName("");
      setCreating(false);
      if (created) await switchTo(created.slug);
    }
  }

  async function deleteProject(p: ProjectInfo) {
    if (!confirm(
      `Delete project "${p.name}"? Its scripts and data are archived under ` +
      "Legacy (nothing is destroyed).",
    )) return;
    await post(`/api/projects/${p.slug}`, undefined, "DELETE");
  }

  const active = data?.projects.find((p) => p.slug === data.active);

  return (
    <div
      ref={rootRef}
      style={{ position: "relative", display: "inline-flex" }}
      // Grace-delayed close on mouse-away; re-enter cancels. Suspended
      // while the create field is open — drifting the cursor out must
      // not eat a half-typed project name.
      onMouseLeave={() => { if (open && !creating) scheduleClose(); }}
      onMouseEnter={cancelClose}
    >
      <button
        className="hbtn project-chip"
        type="button"
        title={active ? `Project: ${active.name}` : "Projects"}
        aria-label="Switch project"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 5,
          maxWidth: 140, paddingLeft: 7, paddingRight: 7, width: "auto",
        }}
      >
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" style={{ flexShrink: 0 }}>
          <path d="M1 4h5l1.5 1.5H15V13H1V4z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
        </svg>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11 }}>
          {active?.name ?? "…"}
        </span>
        <svg viewBox="0 0 8 6" width="7" height="5" aria-hidden="true" style={{ flexShrink: 0, opacity: 0.7 }}>
          <path d="M1 1l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      </button>

      {open && (
        <div
          className="project-menu"
          style={{
            position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 60,
            minWidth: 210, maxHeight: 320, overflowY: "auto",
            background: "var(--voitta-bg, #fff)",
            border: "1px solid var(--voitta-border, #ccc)",
            borderRadius: 8, boxShadow: "0 4px 16px rgba(0,0,0,0.18)",
            padding: 4, fontSize: 12,
          }}
        >
          {data?.projects.map((p) => (
            <div
              key={p.slug}
              style={{
                display: "flex", alignItems: "center", gap: 6,
                padding: "5px 8px", borderRadius: 5, cursor: "pointer",
                background: p.slug === data.active ? "var(--voitta-accent-bg, rgba(120,120,255,0.12))" : undefined,
              }}
              onClick={() => void switchTo(p.slug)}
            >
              <span style={{ width: 12, textAlign: "center" }}>
                {p.slug === data.active ? "✓" : ""}
              </span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {p.name}
              </span>
              {!p.is_legacy && (
                <button
                  type="button"
                  title={`Delete project "${p.name}" (archives into Legacy)`}
                  aria-label={`Delete project ${p.name}`}
                  disabled={busy}
                  onClick={(e) => { e.stopPropagation(); void deleteProject(p); }}
                  style={{
                    border: 0, background: "none", cursor: "pointer",
                    color: "inherit", opacity: 0.5, fontSize: 11, padding: "0 2px",
                  }}
                >
                  ✕
                </button>
              )}
            </div>
          ))}

          <div style={{ borderTop: "1px solid var(--voitta-border, #ccc)", margin: "4px 0" }} />

          {!creating ? (
            <div
              style={{ padding: "5px 8px", borderRadius: 5, cursor: "pointer", opacity: 0.85 }}
              onClick={() => setCreating(true)}
            >
              ＋ New project…
            </div>
          ) : (
            <div style={{ display: "flex", gap: 4, padding: "4px 6px" }}>
              <input
                autoFocus
                type="text"
                value={newName}
                placeholder="Project name"
                onChange={(e) => setNewName(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void createProject();
                  if (e.key === "Escape") { setCreating(false); setNewName(""); }
                }}
                style={{
                  flex: 1, fontSize: 12, padding: "3px 6px",
                  border: "1px solid var(--voitta-border, #ccc)", borderRadius: 4,
                  background: "transparent", color: "inherit",
                }}
              />
              <button
                type="button"
                disabled={busy || !newName.trim()}
                onClick={() => void createProject()}
                style={{ fontSize: 11, cursor: "pointer" }}
              >
                Create
              </button>
            </div>
          )}

          {err && (
            <div style={{ padding: "4px 8px", color: "var(--voitta-err-fg, #b00020)", fontSize: 11 }}>
              {err}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
