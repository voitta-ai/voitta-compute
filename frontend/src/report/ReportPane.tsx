// Report pane chrome: dynamic tab bar, body, collapse handle.
// Positioning owned by Drawer's root data-layout; CSS in report.css.

import { useEffect, useRef } from "react";
import { useRecoilState, useSetRecoilState } from "recoil";
import {
  activeTabState,
  reportCollapsedState,
  reportLoadingState,
  reportsState,
  workspaceTabOpenState,
} from "./state";
import ReportRenderer from "./ReportRenderer";
import WorkspacePanel from "../workspace/WorkspacePanel";

interface Props {
  backendOrigin: string;
  chatOpen: boolean;
  onResizeDown: (e: React.PointerEvent<HTMLDivElement>) => void;
  onResizeDblClick: () => void;
}

export default function ReportPane({
  backendOrigin,
  chatOpen,
  onResizeDown,
  onResizeDblClick,
}: Props) {
  const [reports, setReports] = useRecoilState(reportsState);
  const [activeTab, setActiveTab] = useRecoilState(activeTabState);
  const [workspaceOpen, setWorkspaceOpen] = useRecoilState(workspaceTabOpenState);
  const [collapsed, setCollapsed] = useRecoilState(reportCollapsedState);
  const [loading] = useRecoilState(reportLoadingState);
  const setCollapsedOnly = useSetRecoilState(reportCollapsedState);

  const hasTabs = workspaceOpen || reports.length > 0;

  // Report active tab to backend so the LLM can query it
  const lastReportedTab = useRef<string | null>(null);
  useEffect(() => {
    if (!hasTabs) return;
    const tab = validTab ?? null;
    if (tab === lastReportedTab.current) return;
    lastReportedTab.current = tab;
    const report = reports.find((r) => r.render_id === tab);
    fetch(`${backendOrigin}/api/workspace/active`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tab, name: report?.name ?? null, title: report?.title ?? null }),
    }).catch(() => {/* ignore */});
  });

  if (!hasTabs) return null;

  // Derive a safe active tab: if current activeTab is stale/null, fall back.
  const validTab: string = (() => {
    if (activeTab === "workspace" && workspaceOpen) return "workspace";
    if (activeTab && reports.some((r) => r.render_id === activeTab)) return activeTab;
    if (workspaceOpen) return "workspace";
    return reports[0]?.render_id ?? "workspace";
  })();

  function closeTab(id: string) {
    if (id === "workspace") {
      setWorkspaceOpen(false);
      if (activeTab === "workspace") {
        setActiveTab(reports[0]?.render_id ?? null);
      }
    } else {
      const idx = reports.findIndex((r) => r.render_id === id);
      const next = reports[idx + 1] ?? reports[idx - 1];
      setReports((prev) => prev.filter((r) => r.render_id !== id));
      if (activeTab === id) {
        if (next) setActiveTab(next.render_id);
        else if (workspaceOpen) setActiveTab("workspace");
        else setActiveTab(null);
      }
    }
  }

  const activeReport = reports.find((r) => r.render_id === validTab) ?? null;

  return (
    <>
      {!chatOpen && !collapsed && (
        <div
          className="report-resizer"
          role="separator"
          aria-orientation="vertical"
          title="Drag to resize · double-click to reset"
          onPointerDown={onResizeDown}
          onDoubleClick={onResizeDblClick}
        />
      )}
      <aside
        className={`report-pane${collapsed ? " is-collapsed" : ""}`}
        role="complementary"
        aria-hidden={collapsed}
        // @ts-ignore — inert is not yet in React's types but is supported in all modern browsers
        inert={collapsed ? "" : undefined}
      >
        <header className="report-header">
          <nav className="report-tabs" aria-label="Pane tabs">
            {workspaceOpen && (
              <button
                className={`report-tab${validTab === "workspace" ? " active" : ""}`}
                type="button"
                onClick={() => setActiveTab("workspace")}
                aria-selected={validTab === "workspace"}
              >
                <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                  <path d="M1 4h5l1.5 1.5H15V13H1V4z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
                </svg>
                Workspace
                <span
                  className="tab-close"
                  role="button"
                  aria-label="Close Workspace tab"
                  onClick={(e) => { e.stopPropagation(); closeTab("workspace"); }}
                >×</span>
              </button>
            )}
            {reports.map((r) => (
              <button
                key={r.render_id}
                className={`report-tab${validTab === r.render_id ? " active" : ""}`}
                type="button"
                onClick={() => setActiveTab(r.render_id)}
                aria-selected={validTab === r.render_id}
                title={r.title ?? r.name}
              >
                <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                  <rect x="2" y="2" width="12" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.4" />
                  <line x1="5" y1="5.5" x2="11" y2="5.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                  <line x1="5" y1="8" x2="11" y2="8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                  <line x1="5" y1="10.5" x2="8.5" y2="10.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                </svg>
                <span className="tab-label">{r.title ?? r.name}</span>
                <span
                  className="tab-close"
                  role="button"
                  aria-label={`Close ${r.title ?? r.name}`}
                  onClick={(e) => { e.stopPropagation(); closeTab(r.render_id); }}
                >×</span>
              </button>
            ))}
          </nav>
          <span className="spacer" />
          <button
            className="hbtn"
            type="button"
            title="Collapse"
            aria-label="Collapse pane"
            onClick={() => setCollapsedOnly(true)}
          >
            ×
          </button>
        </header>

        <div className="report-body">
          {workspaceOpen && (
            <div className={`report-tab-panel${validTab === "workspace" ? " active" : ""}`}>
              <WorkspacePanel
                backendOrigin={backendOrigin}
                embedded
              />
            </div>
          )}

          {reports.map((r) => (
            <div
              key={r.render_id}
              className={`report-tab-panel${validTab === r.render_id ? " active" : ""}`}
              data-report-id={r.render_id}
              data-active={validTab === r.render_id ? "true" : undefined}
            >
              {loading && validTab === r.render_id && (
                <div className="report-loading-overlay">
                  <svg className="report-spinner" viewBox="0 0 40 40" width="40" height="40" aria-label="Loading…">
                    <circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" strokeWidth="3"
                      strokeDasharray="60 40" strokeLinecap="round" />
                  </svg>
                </div>
              )}
              <ReportRenderer backendOrigin={backendOrigin} report={r} />
            </div>
          ))}
        </div>
      </aside>

      {collapsed && (
        <button
          type="button"
          className="report-handle"
          title="Reopen pane"
          aria-label="Reopen pane"
          onClick={() => setCollapsed(false)}
        >
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
            <path d="M6 3h9l4 4v14H6z M14 3v5h5"
              fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </>
  );
}
