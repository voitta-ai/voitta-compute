// Report pane — fills the viewport edge opposite the chat drawer.
//
// chat-right layout (default): report on left, chat on right.
//     ┌──────────────────────────────────────────┬───────────┐
//     │  ReportPane (iframe + edit + × buttons)  │  Drawer   │
//     └──────────────────────────────────────────┴───────────┘
//
// chat-left layout: chat on left, report on right.
//     ┌───────────┬──────────────────────────────────────────┐
//     │  Drawer   │  ReportPane (iframe + edit + × buttons)  │
//     └───────────┴──────────────────────────────────────────┘
//
// When the chat drawer is collapsed the report pane stretches to fill
// the full viewport (drawerWidth = 0). Both panes live inside the host's
// Shadow DOM so host-page styles don't bleed in.

import { useEffect, useRef, useState } from "preact/hooks";

import { getBackendOrigin } from "../lib/bridge";
import { setActiveReportIframe } from "../lib/report-iframe";

interface ReportInfo {
  url: string;
  report_id: string;
  title?: string;
}

interface Props {
  info: ReportInfo;
  // Collapse to a handle. The iframe stays mounted under display:none
  // so the Bokeh session, scroll position, and any in-iframe widget
  // state survive. We don't expose a separate "destroy" path — the
  // iframe is cheap and re-opening via show_holoviz_report would
  // otherwise re-run the report unnecessarily.
  onCollapse: () => void;
  collapsed: boolean;
  drawerWidth: number;
  layout?: "chat-right" | "chat-left";
}

// Append (or remove) ?editable=true on the iframe URL. Toggling reloads
// the iframe — EditableTemplate bakes the editable flag in at template
// init, so a runtime toggle without a reload would mean shipping our own
// JS shim. The reload is acceptable.
function withEditable(url: string, on: boolean): string {
  if (!on) return url;
  return url + (url.includes("?") ? "&" : "?") + "editable=true";
}

export function ReportPane({ info, onCollapse, collapsed, drawerWidth, layout = "chat-right" }: Props) {
  const title = info.title || `Report ${info.report_id || "(unnamed)"}`;
  const [editing, setEditing] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  // Title-click "data sources" popover. Lists the canonical
  // upstream-artefact refs the report's last run resolved through
  // ctx.ensure_local — read-only, click to dismiss. Empty state shows
  // a friendly placeholder rather than hiding the popover so the user
  // can confirm "yes, this report has no upstream sources" vs "I
  // haven't run it yet".
  const [refsOpen, setRefsOpen] = useState(false);
  const [refsList, setRefsList] = useState<string[] | null>(null);
  const [refsError, setRefsError] = useState<string | null>(null);

  // Lazy-fetch on first open. Re-fetch on every open after that so a
  // re-render's new refs surface without a manual refresh — the
  // endpoint is cheap (single JSON file read).
  useEffect(() => {
    if (!refsOpen) return;
    const id = info.report_id;
    if (!id) return;
    const origin = getBackendOrigin();
    if (!origin) return;

    // Reset to Loading state on every open so the user sees fresh
    // feedback (stale "no refs" or error from a previous open would
    // otherwise show until the new fetch resolves).
    setRefsError(null);
    setRefsList(null);

    // AbortController lets us cancel a slow request if the user
    // closes + reopens the popover quickly — without this, the older
    // fetch could resolve last and overwrite the newer one's result.
    const ac = new AbortController();
    fetch(`${origin}/api/artifacts/python_storage/reports/${encodeURIComponent(id)}/refs`, {
      credentials: "include",
      signal: ac.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { refs?: string[] }) => {
        setRefsList(Array.isArray(data.refs) ? data.refs : []);
      })
      .catch((e: any) => {
        if (e?.name === "AbortError") return;
        setRefsError(e?.message || String(e));
        setRefsList([]);
      });
    return () => ac.abort();
  }, [refsOpen, info.report_id]);

  // Dismiss popover on outside click or Escape.
  const refsRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!refsOpen) return;
    function onDown(e: MouseEvent) {
      const path = typeof (e as any).composedPath === "function"
        ? ((e as any).composedPath() as EventTarget[])
        : [];
      if (refsRef.current && !path.includes(refsRef.current)) setRefsOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setRefsOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [refsOpen]);

  async function copyRef(ref: string) {
    try { await navigator.clipboard.writeText(ref); }
    catch { /* clipboard denied — ignore, the ref stays visible */ }
  }

  // Forward an action to the iframe's postMessage listener (installed
  // server-side via Bokeh CustomJS in panel_app.py). The iframe origin is
  // the FastAPI backend, not the host page — we use '*' so the same code
  // works on every host. The iframe-side handler verifies
  // `e.source === window.parent`.
  const send = (action: "undo" | "reset") => {
    iframeRef.current?.contentWindow?.postMessage({ voittaAction: action }, "*");
  };

  // Listen for render-lifecycle events from the iframe shim and forward
  // them to the backend so a pending show_holoviz_report await can wake
  // up (or the persisted log gets the entry). The iframe → parent leg
  // runs on every report load; the parent → backend leg is fire-and-forget
  // (we don't surface failures — the LLM will see the absence of a 'ready'
  // event when its tool times out).
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      const data = e.data;
      if (
        !data ||
        typeof data !== "object" ||
        (data as { type?: string }).type !== "voitta_report_event"
      ) {
        return;
      }
      // Only accept events from our own iframe.
      if (e.source !== iframeRef.current?.contentWindow) return;
      // The "voitta_report_event" envelope is shared by render-lifecycle
      // events AND by RPC responses the parent itself solicited
      // (measure_response, screenshot_response, three_capture_response).
      // Only the lifecycle kinds belong on the /api/report-render-events
      // endpoint; the RPC responses are handled by their own promise
      // listeners in primitives.ts and would 400 here.
      const kind = (data as { kind?: string }).kind;
      if (kind !== "ready" && kind !== "error" && kind !== "inventory") {
        return;
      }
      const origin = getBackendOrigin();
      if (!origin) return;
      const url = origin.replace(/\/$/, "") + "/api/report-render-events";
      // The shim has already shaped the payload; pass it through verbatim
      // (minus the envelope `type` key). Fire and forget.
      const payload: Record<string, unknown> = {};
      for (const k of Object.keys(data as Record<string, unknown>)) {
        if (k !== "type") payload[k] = (data as Record<string, unknown>)[k];
      }
      void fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
        keepalive: true,
      }).catch(() => {});
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // Reset to viewing mode whenever a new report is shown. Without this,
  // when the LLM calls show_holoviz_report on a different report (or
  // re-renders the same one), the pane stays in edit mode and the
  // ?editable=true URL hides the new content behind the editable
  // template's grid. Edit mode is per-report intent; new report → fresh
  // viewing.
  useEffect(() => {
    setEditing(false);
  }, [info.report_id, info.url]);

  // Register this iframe in the global registry so the
  // `screenshot_report` browser primitive can find it without us having
  // to thread refs through the widget.
  useEffect(() => {
    setActiveReportIframe(iframeRef.current, {
      report_id: info.report_id,
      url: info.url,
      title: info.title,
    });
    return () => setActiveReportIframe(null, null);
  }, [info.report_id, info.url, info.title]);

  return (
    <section
      class={`report-pane${collapsed ? " is-collapsed" : ""}`}
      role="complementary"
      aria-label={title}
      // Slide off-screen via CSS transform when collapsed (see
      // .report-pane.is-collapsed in styles.css). Transform-based hiding
      // keeps the iframe document alive — Bokeh session, scroll position,
      // any in-iframe widget state survive collapse/expand cycles, just
      // like display:none would, but with a slide animation that mirrors
      // the chat drawer.
      style={layout === "chat-left"
        ? { left: `${drawerWidth}px` }
        : { right: `${drawerWidth}px` }}
    >
      <header class="report-header">
        <span
          class="report-title is-clickable"
          title="Click to see upstream data sources"
          role="button"
          tabIndex={0}
          onClick={() => setRefsOpen((v) => !v)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setRefsOpen((v) => !v); }}
        >
          {title}
        </span>
        <span class="report-id">{info.report_id}</span>
        {refsOpen && (
          <div class="report-refs-popover" ref={refsRef} role="dialog" aria-label="Upstream data sources">
            <div class="report-refs-header">Upstream data sources</div>
            {refsError && <div class="report-refs-error">Error: {refsError}</div>}
            {!refsError && refsList === null && <div class="report-refs-loading">Loading…</div>}
            {!refsError && refsList !== null && refsList.length === 0 && (
              <div class="report-refs-empty">
                No upstream refs recorded for the last run.
                <div class="report-refs-empty-sub">
                  Reports using <code>ctx.ensure_local(…)</code> register their data sources here.
                </div>
              </div>
            )}
            {!refsError && refsList !== null && refsList.length > 0 && (
              <ul class="report-refs-list">
                {refsList.map((ref) => (
                  <li key={ref} class="report-refs-item" onClick={() => copyRef(ref)} title="Click to copy">
                    {ref}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <span class="spacer" />
        {editing && (
          <>
            <button
              class="report-action"
              type="button"
              title="Undo last layout change"
              aria-label="Undo"
              onClick={() => send("undo")}
            >
              {/* Curved-back arrow — the standard undo glyph used in text
                  editors, file managers, etc. (Lucide "undo-2" geometry). */}
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">
                <path d="M9 14 4 9l5-5" />
                <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H8" />
              </svg>
            </button>
            <button
              class="report-action"
              type="button"
              title="Reset layout to original"
              aria-label="Reset layout"
              onClick={() => send("reset")}
            >
              {/* Counter-clockwise circular arrow — distinct from a plain
                  refresh icon thanks to the explicit "back to start" turn
                  at the top-left. (Lucide "rotate-ccw" geometry.) */}
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">
                <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
                <path d="M3 3v5h5" />
              </svg>
            </button>
          </>
        )}
        <button
          class={`report-edit${editing ? " is-active" : ""}`}
          type="button"
          title={editing ? "Exit edit mode" : "Enter edit mode (drag/resize cards)"}
          aria-label="Toggle edit mode"
          aria-pressed={editing}
          onClick={() => setEditing(v => !v)}
        >
          {/* 4-arrow "move" glyph; coloured background when active */}
          ⇲
        </button>
        <button
          class="report-close"
          type="button"
          title="Collapse report (reopen from the handle on the left edge)"
          aria-label="Collapse report"
          onClick={onCollapse}
        >
          ×
        </button>
      </header>
      <iframe
        ref={iframeRef}
        class="report-iframe"
        src={withEditable(info.url, editing)}
        title={title}
        // The iframe is same-origin only relative to the FastAPI backend
        // (127.0.0.1:12358), not the host page. We don't sandbox so
        // Bokeh's interactive widgets continue to work (they need scripts
        // + same-origin XHR back to the backend).
      />
    </section>
  );
}
