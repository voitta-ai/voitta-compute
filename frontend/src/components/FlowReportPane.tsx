// Flow report pane — Preact wrapper. The diagram itself is real React
// (ReactFlow + d3-zoom only work under real React, not preact/compat).
//
// Strategy: this Preact component renders the outer chrome (header,
// close button) and an empty `<div>`. On mount, we lazy-load React and
// the FlowDiagram component, then mount FlowDiagram as a React root
// (`createRoot`) inside that div. Props are passed via re-render
// each time the definition changes. Cleanup unmounts the root on
// component unmount.
//
// Why lazy-load: real React + ReactDOM is ~140 KB gzipped; loading
// it only when a flow is shown keeps the chat-only path cheap.

import { useEffect, useRef } from "preact/hooks";

import { getBackendOrigin } from "../lib/bridge";
import { log } from "../lib/logger";
import type { FlowDefinition } from "../lib/flow-types";

export interface FlowReportInfo {
  report_id: string;
  title?: string;
  render_id?: string;
  definition: FlowDefinition;
}

interface Props {
  info: FlowReportInfo;
  onCollapse: () => void;
  collapsed: boolean;
  drawerWidth: number;
  layout?: "chat-right" | "chat-left";
}

function postEvent(payload: Record<string, unknown>): void {
  const origin = getBackendOrigin();
  if (!origin) return;
  const url = origin.replace(/\/$/, "") + "/api/report-render-events";
  void fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    credentials: "include",
    keepalive: true,
  }).catch(() => {});
}

export function FlowReportPane({
  info,
  onCollapse,
  collapsed,
  drawerWidth,
  layout = "chat-right",
}: Props) {
  const title = info.title || `Flow ${info.report_id || "(unnamed)"}`;
  const hostRef = useRef<HTMLDivElement | null>(null);
  // The React root and the FlowDiagram component live in module-local
  // refs so re-renders of this Preact component can `root.render(...)`
  // with fresh props without re-creating the root.
  const rootRef = useRef<any>(null);
  const FlowDiagramRef = useRef<any>(null);
  const ReactRef = useRef<any>(null);

  const handleReady = () => {
    if (!info.render_id) return;
    postEvent({
      type: "voitta_report_event",
      kind: "ready",
      render_id: info.render_id,
      report_id: info.report_id,
      ts: Date.now() / 1000,
      source: "flow:render",
    });
  };
  const handleError = (err: Error) => {
    log.warn("flow-report", "render failed", { err: err.message });
    if (!info.render_id) return;
    postEvent({
      type: "voitta_report_event",
      kind: "error",
      render_id: info.render_id,
      report_id: info.report_id,
      ts: Date.now() / 1000,
      source: "flow:render",
      message: err.message,
      stack: err.stack,
    });
  };

  // Mount the React root once on first render. Re-render with new
  // props whenever `info.definition` changes.
  useEffect(() => {
    if (!hostRef.current) return;
    let cancelled = false;

    (async () => {
      // Lazy-load real React and the FlowDiagram component. Two
      // dynamic imports so they ship in the same chunk (the bundler
      // collapses them).
      const [reactModule, reactDomModule, flowModule] = await Promise.all([
        import("react"),
        import("react-dom/client"),
        import("./FlowDiagram"),
      ]);
      if (cancelled || !hostRef.current) return;

      ReactRef.current = reactModule;
      FlowDiagramRef.current = flowModule.FlowDiagram;

      if (!rootRef.current) {
        rootRef.current = reactDomModule.createRoot(hostRef.current);
        log.info("flow-report", "react root mounted");
      }
      if (cancelled) return;
      rootRef.current.render(
        reactModule.createElement(flowModule.FlowDiagram, {
          definition: info.definition,
          onReady: handleReady,
          onError: handleError,
        }),
      );
    })();

    return () => {
      cancelled = true;
    };
    // We deliberately depend on `info.definition` (the value we
    // forward into the React render). Other deps are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info.definition, info.render_id, info.report_id]);

  // Unmount the React root when this Preact component unmounts.
  useEffect(() => {
    return () => {
      if (rootRef.current) {
        log.info("flow-report", "react root unmount");
        try {
          rootRef.current.unmount();
        } catch (e) {
          // Defensive — unmount can throw if the host node is gone.
        }
        rootRef.current = null;
      }
    };
  }, []);

  return (
    <section
      class={`report-pane flow-pane${collapsed ? " is-collapsed" : ""}`}
      role="complementary"
      aria-label={title}
      style={layout === "chat-left"
        ? { left: `${drawerWidth}px` }
        : { right: `${drawerWidth}px` }}
    >
      <header class="report-header">
        <span class="report-title" title={info.report_id}>{title}</span>
        <span class="report-id">{info.report_id}</span>
        <span class="spacer" />
        <button
          class="report-close"
          type="button"
          title="Collapse flow"
          aria-label="Collapse flow"
          onClick={onCollapse}
        >
          ×
        </button>
      </header>
      <div class="flow-body">
        {/* Host for the React-managed FlowDiagram island. The
            `dangerouslySetInnerHTML` is the canonical way to tell
            Preact's reconciler "I own this subtree, don't diff its
            children." Without it, every parent re-render (each keystroke
            in the composer triggers one) re-checks this div's contents
            and can interfere with React's reconciliation — that's the
            UI-freeze / lost-keystroke symptom. */}
        <div
          ref={hostRef}
          class="flow-react-host"
          style={{ width: "100%", height: "100%" }}
          dangerouslySetInnerHTML={{ __html: "" }}
        />
      </div>
    </section>
  );
}
