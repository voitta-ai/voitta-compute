// Drawer chrome: the floating handle (visible when closed), the
// sliding panel, the header with brand + provider chip + button tray,
// and view switching (chat / settings). Layout (left/right) comes from
// the persisted settings cache.

import { useCallback, useEffect, useRef, useState } from "react";
import { useChatInteract, useChatSession } from "@chainlit/react-client";
import { useSetRecoilState } from "recoil";
import { useSettings } from "./lib/useSettings";
import { saveSettings } from "./lib/settings";
import ChatPane from "./ChatPane";
import SettingsView from "./SettingsView";
import CallFnRouter from "./lib/CallFnRouter";
import ReportPane from "./report/ReportPane";
import { activeTabState, reportCollapsedState, workspaceTabOpenState } from "./report/state";

type View = "chat" | "settings";

const STORE_OPEN = "voitta-bkmk-open";
const STORE_WIDTH = "voitta-bkmk-width";
const DEFAULT_WIDTH_PX = 420;
const MIN_WIDTH_PX = 280;
const MAX_WIDTH_VW = 92;

function clampWidth(px: number): number {
  const max = Math.floor(window.innerWidth * (MAX_WIDTH_VW / 100));
  return Math.max(MIN_WIDTH_PX, Math.min(max, px));
}

function loadInitialWidth(): number {
  try {
    const saved = parseInt(localStorage.getItem(STORE_WIDTH) || "", 10);
    if (Number.isFinite(saved) && saved > 0) return clampWidth(saved);
  } catch {
    /* ignore */
  }
  return DEFAULT_WIDTH_PX;
}

interface Props {
  backendOrigin: string;
}

export default function Drawer({ backendOrigin }: Props) {
  const settings = useSettings();
  const { clear } = useChatInteract();
  const { connect } = useChatSession();
  const setWorkspaceOpen = useSetRecoilState(workspaceTabOpenState);
  const setActiveTab = useSetRecoilState(activeTabState);
  const setReportCollapsed = useSetRecoilState(reportCollapsedState);
  const [open, setOpen] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(STORE_OPEN) !== "0";
    } catch {
      return true;
    }
  });
  const currentHasKey = Boolean(settings.has_api_keys[settings.provider]);
  const [view, setView] = useState<View>(currentHasKey ? "chat" : "settings");
  const [width, setWidth] = useState<number>(loadInitialWidth);
  const [resizing, setResizing] = useState(false);
  const widthRef = useRef(width);
  widthRef.current = width;
  const layoutRef = useRef(settings.layout);
  layoutRef.current = settings.layout;

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE_OPEN, open ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [open]);

  useEffect(() => {
    try {
      localStorage.setItem(STORE_WIDTH, String(width));
    } catch {
      /* ignore */
    }
  }, [width]);

  // Re-clamp on viewport resize so width never exceeds 92vw after the
  // window shrinks.
  useEffect(() => {
    const onWindowResize = () => setWidth((w) => clampWidth(w));
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, []);


  // Pointer-driven drag. The resizer sits on the inner edge of the
  // drawer — left edge in chat-right layout, right edge in chat-left.
  // Drag direction is inverted accordingly. Pointer capture lets us
  // keep tracking even if the cursor leaves the 6px grip strip.
  const onResizeDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    setResizing(true);
    const startX = e.clientX;
    const startW = widthRef.current;
    const chatLeft = layoutRef.current === "chat-left";

    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      setWidth(clampWidth(chatLeft ? startW + dx : startW - dx));
    };
    const up = (ev: PointerEvent) => {
      setResizing(false);
      try {
        target.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  }, []);

  const onResizeDblClick = useCallback(() => {
    setWidth(DEFAULT_WIDTH_PX);
  }, []);

  function toggleView(target: View) {
    setView((v) => (v === target ? "chat" : target));
  }

  const providerChip = settings.provider || "—";

  return (
    <div
      className="root"
      data-open={open ? "true" : "false"}
      data-resizing={resizing ? "true" : "false"}
      data-layout={settings.layout}
      data-theme={settings.theme}
      style={{ ["--voitta-pane-width" as string]: width + "px" } as React.CSSProperties}
    >
      {/* floating handle — only visible when drawer is closed */}
      <button
        className="handle"
        type="button"
        title="Open chat"
        aria-label="Open chat"
        onClick={() => setOpen(true)}
      >
        <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
          <path
            d="M4 4h16v12H7l-3 3V4z"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinejoin="round"
          />
        </svg>
      </button>

      <aside className="drawer" role="complementary" aria-label="Voitta chat">
        <div
          className="resizer"
          role="separator"
          aria-orientation="vertical"
          title="Drag to resize · double-click to reset"
          onPointerDown={onResizeDown}
          onDoubleClick={onResizeDblClick}
        />
        <header>
          <span className="brand-mark" aria-hidden="true">
            ●
          </span>
          <span className="brand-name">Voitta</span>
          <button
            className="provider-chip"
            type="button"
            title="Settings"
            onClick={() => toggleView("settings")}
          >
            {providerChip}
          </button>
          <span className="spacer" />
          {view === "chat" && (
            <>
              <button
                className="hbtn"
                type="button"
                title="Workspace"
                aria-label="Open workspace"
                onClick={() => { setWorkspaceOpen(true); setActiveTab("workspace"); setReportCollapsed(false); }}
              >
                <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                  <path d="M1 4h5l1.5 1.5H15V13H1V4z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
                </svg>
              </button>
              <button
                className="hbtn theme-toggle"
                type="button"
                title={(() => {
                  const eff = settings.theme === "auto"
                    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
                    : settings.theme;
                  return eff === "dark" ? "Switch to light" : "Switch to dark";
                })()}
                aria-label="Toggle color scheme"
                onClick={() => {
                  const eff = settings.theme === "auto"
                    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
                    : settings.theme;
                  saveSettings(backendOrigin, { theme: eff === "dark" ? "light" : "dark" });
                }}
              >
                {(() => {
                  const eff = settings.theme === "auto"
                    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
                    : settings.theme;
                  return eff === "dark"
                    ? (
                      // sun
                      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                        <circle cx="8" cy="8" r="3" />
                        <line x1="8" y1="1" x2="8" y2="3" />
                        <line x1="8" y1="13" x2="8" y2="15" />
                        <line x1="1" y1="8" x2="3" y2="8" />
                        <line x1="13" y1="8" x2="15" y2="8" />
                        <line x1="2.93" y1="2.93" x2="4.34" y2="4.34" />
                        <line x1="11.66" y1="11.66" x2="13.07" y2="13.07" />
                        <line x1="13.07" y1="2.93" x2="11.66" y2="4.34" />
                        <line x1="4.34" y1="11.66" x2="2.93" y2="13.07" />
                      </svg>
                    )
                    : (
                      // moon
                      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M13.5 10A6 6 0 0 1 6 2.5a5.5 5.5 0 1 0 7.5 7.5z" />
                      </svg>
                    );
                })()}
              </button>
              <button
                className="hbtn"
                type="button"
                title="Clear conversation"
                aria-label="Clear conversation"
                onClick={() => { clear(); connect({ userEnv: {} }); }}
              >
                ↻
              </button>
            </>
          )}
          <button
            className={`hbtn ${view === "settings" ? "active" : ""}`}
            type="button"
            title={view === "settings" ? "Back to chat" : "Settings"}
            aria-label={view === "settings" ? "Back to chat" : "Settings"}
            onClick={() => toggleView("settings")}
          >
            {view === "settings" ? "←" : "⚙"}
          </button>
          <button
            className="hbtn"
            type="button"
            title="Close"
            aria-label="Close"
            onClick={() => setOpen(false)}
          >
            ×
          </button>
        </header>

        {/* Keep ChatPane mounted so the composer's draft text survives view switches */}
        <div className="chat-wrap" style={view !== "chat" ? { display: "none" } : undefined}>
          <ChatPane backendOrigin={backendOrigin} hasApiKey={currentHasKey} />
        </div>
        {view === "settings" && (
          <SettingsView backendOrigin={backendOrigin} onClose={() => setView("chat")} />
        )}
      </aside>

      <ReportPane
        backendOrigin={backendOrigin}
        chatOpen={open}
        onResizeDown={onResizeDown}
        onResizeDblClick={onResizeDblClick}
      />
      <CallFnRouter />
    </div>
  );
}
