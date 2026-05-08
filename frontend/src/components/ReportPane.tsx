// Left-side report pane.
//
// Renders a HoloViz Panel report in an iframe, sized to fill the page
// from the left edge up to the chat drawer. Layout — when a report is
// active and the chat drawer is open:
//
//     ┌──────────────────────────────────────────┬───────────┐
//     │  ReportPane (iframe + edit + × buttons)  │  Drawer   │
//     │                                          │  (chat)   │
//     └──────────────────────────────────────────┴───────────┘
//
// When the chat drawer is collapsed (handle visible), the report pane
// stretches all the way to the right edge (drawerWidth = 0). Both panes
// live inside the host's Shadow DOM so host-page styles don't bleed in.

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
}

// Append (or remove) ?editable=true on the iframe URL. Toggling reloads
// the iframe — EditableTemplate bakes the editable flag in at template
// init, so a runtime toggle without a reload would mean shipping our own
// JS shim. The reload is acceptable.
function withEditable(url: string, on: boolean): string {
  if (!on) return url;
  return url + (url.includes("?") ? "&" : "?") + "editable=true";
}

export function ReportPane({ info, onCollapse, collapsed, drawerWidth }: Props) {
  const title = info.title || `Report ${info.report_id || "(unnamed)"}`;
  const [editing, setEditing] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

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
      style={{ right: `${drawerWidth}px` }}
    >
      <header class="report-header">
        <span class="report-title" title={info.url}>
          {title}
        </span>
        <span class="report-id">{info.report_id}</span>
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
