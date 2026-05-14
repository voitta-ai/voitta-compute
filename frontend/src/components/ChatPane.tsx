import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { streamChat, type ChatMessage, type TurnItem } from "../lib/api";
import { fetchAuthStatus, loginWithApiKey } from "../lib/auth";
import { getSessionId } from "../lib/bridge";
import { log } from "../lib/logger";
import type { RichOutput } from "../lib/plot-spec";
import { setReportSink, setRichOutputSink } from "../lib/primitives-buffers";
import {
  activeApiKey,
  activeModel,
  loadSettings,
  subscribeSettings,
  type Settings,
} from "../lib/settings";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { LogsView } from "./LogsView";
import { ReportPane } from "./ReportPane";
import { SettingsView } from "./SettingsView";
import { ArtifactsView } from "./ArtifactsView";

interface ReportInfo {
  url: string;
  report_id: string;
  title?: string;
}

type View = "chat" | "settings" | "logs";

const STORAGE_OPEN_KEY = "voitta-bkmk-open";

const DEFAULT_BRAND_NAME = "Voitta";
const STORAGE_WIDTH_KEY = "voitta-bkmk-width";
const DEFAULT_WIDTH_PX = 400;
const MIN_WIDTH_PX = 280;
const MAX_WIDTH_VW = 92;

const PROVIDER_CHIP: Record<Settings["provider"], string> = {
  anthropic: "anthropic",
  openai: "openai",
  gemini: "gemini",
};

function clampWidth(px: number): number {
  const max = Math.floor(window.innerWidth * (MAX_WIDTH_VW / 100));
  return Math.max(MIN_WIDTH_PX, Math.min(max, px));
}

function loadInitialOpen(): boolean {
  try {
    return sessionStorage.getItem(STORAGE_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

function loadInitialWidth(): number {
  try {
    const saved = parseInt(localStorage.getItem(STORAGE_WIDTH_KEY) || "", 10);
    if (Number.isFinite(saved) && saved > 0) return clampWidth(saved);
  } catch {
    /* ignore */
  }
  return DEFAULT_WIDTH_PX;
}

interface Props {
  backendOrigin: string;
  agentName?: string;
  hideBrand?: boolean;
}

// Helpers that operate on a TurnItem[] in stream order, returning a *new*
// array (kept outside the component so they don't allocate per render).
function appendText(items: TurnItem[], text: string): TurnItem[] {
  if (!text) return items;
  const last = items[items.length - 1];
  if (last && last.kind === "text") {
    return [...items.slice(0, -1), { kind: "text", text: last.text + text }];
  }
  return [...items, { kind: "text", text }];
}

function appendToolStart(items: TurnItem[], id: string, name: string): TurnItem[] {
  return [...items, { kind: "tool", id, name, status: "running" }];
}

function appendRich(items: TurnItem[], output: RichOutput): TurnItem[] {
  return [...items, { kind: "rich", output }];
}

function updateToolEnd(
  items: TurnItem[],
  info: {
    id: string;
    name?: string;
    ok: boolean;
    latency_ms?: number;
    error?: { message?: string } | null;
    input?: unknown;
    result_preview?: string | null;
  },
): TurnItem[] {
  return items.map((it) =>
    it.kind === "tool" && it.id === info.id
      ? {
          ...it,
          status: info.ok ? "ok" : "error",
          latency_ms: info.latency_ms,
          error_message: info.error?.message ?? null,
          input: info.input,
          result_preview: info.result_preview ?? null,
        }
      : it,
  );
}

function itemsToContent(items: TurnItem[]): string {
  return items
    .filter((it): it is Extract<TurnItem, { kind: "text" }> => it.kind === "text")
    .map((it) => it.text)
    .join("");
}

export function ChatPane({ backendOrigin, agentName, hideBrand }: Props) {
  const [open, setOpen] = useState<boolean>(loadInitialOpen);
  const [width, setWidth] = useState<number>(loadInitialWidth);
  const [resizing, setResizing] = useState(false);
  const [view, setView] = useState<View>("chat");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamingItems, setStreamingItems] = useState<TurnItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings>(() => loadSettings());
  const [activeReport, setActiveReport] = useState<ReportInfo | null>(null);

  // Auth state. ``needsAuth`` flips to true when the backend is in
  // non-localhost mode AND the user hasn't logged in yet — we render
  // a login prompt inside the existing chat view in that case rather
  // than mounting a separate top-level page.
  const [needsAuth, setNeedsAuth] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void fetchAuthStatus(backendOrigin)
      .then((s) => {
        if (cancelled) return;
        setNeedsAuth(!(s.localhostMode || s.authenticated));
        setAuthChecked(true);
      })
      .catch(() => {
        if (cancelled) return;
        // Fail open — assume localhost mode if the probe itself
        // dies. The chat will surface the underlying error on its
        // first request.
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [backendOrigin]);
  // Two states for the report pane:
  //   • activeReport === null   → no report (no iframe mounted)
  //   • activeReport && !collapsed → fully visible
  //   • activeReport && collapsed  → iframe stays mounted under display:none,
  //                                   handle button is shown to re-expand
  // Tracking these separately keeps the Panel/Bokeh session alive across
  // collapse/expand without re-running the report.
  const [reportCollapsed, setReportCollapsed] = useState(false);
  const [artifactsOpen, setArtifactsOpen] = useState(false);
  const name = agentName ?? DEFAULT_BRAND_NAME;
  const brand = { name, ariaLabel: `${name} chat pane` };
  const abortRef = useRef<AbortController | null>(null);
  // Mirror of streamingItems used inside the stream callbacks (which close
  // over the value at start time, not the latest React state).
  const itemsRef = useRef<TurnItem[]>([]);
  // Mirror of settings for the same reason.
  const settingsRef = useRef<Settings>(settings);

  function setItems(next: TurnItem[]): void {
    itemsRef.current = next;
    setStreamingItems(next);
  }

  const busy = streaming;

  // Persist open + width.
  useEffect(() => {
    try {
      sessionStorage.setItem(STORAGE_OPEN_KEY, open ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [open]);
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_WIDTH_KEY, String(width));
    } catch {
      /* ignore */
    }
  }, [width]);

  // Subscribe to settings changes (so SettingsView's Save button is
  // immediately visible elsewhere in the pane).
  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);
  useEffect(() => {
    const off = subscribeSettings((s) => {
      settingsRef.current = s;
      setSettings(s);
    });
    return off;
  }, []);

  // Wire the rich-output sink so browser-side primitives (plots, log lines
  // from buffer_eval) append items to the currently-streaming assistant
  // turn. The sink stays registered for the pane's lifetime.
  useEffect(() => {
    setRichOutputSink((output) => {
      setItems(appendRich(itemsRef.current, output));
    });
    return () => setRichOutputSink(null);
  }, []);

  // Wire the report sink — the `show_report` primitive (and therefore the
  // `show_holoviz_report` server tool) sets `activeReport`, which mounts
  // the left-side ReportPane. Calling with `null` would hide it, but we
  // currently only call with a report; the user closes via the × button.
  useEffect(() => {
    setReportSink((info) => {
      setActiveReport(info);
      // A freshly-shown report should always be visible; otherwise an
      // earlier "collapse" persists across navigations and the user
      // sees nothing happen when the LLM calls show_holoviz_report.
      setReportCollapsed(false);
    });
    return () => setReportSink(null);
  }, []);

  // Esc closes pane (only when no input has focus inside the shadow tree).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      const path = e.composedPath?.() || [];
      const fromTypable = path.some((el: any) => {
        if (!el?.tagName) return false;
        const t = (el.tagName as string).toUpperCase();
        return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || el.isContentEditable;
      });
      if (fromTypable) return;
      if (open) {
        e.preventDefault();
        setOpen(false);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Resize drag handler.
  const onResizeDown = useCallback(
    (e: PointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const target = e.currentTarget as HTMLElement;
      target.setPointerCapture(e.pointerId);
      setResizing(true);
      const startX = e.clientX;
      const startW = width;

      const chatLeft = settingsRef.current.layout === "chat-left";
      const move = (ev: PointerEvent) => {
        // chat-right: drag left (negative dx) increases width.
        // chat-left:  drag right (positive dx) increases width.
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
        target.removeEventListener("pointermove", move as any);
        target.removeEventListener("pointerup", up as any);
        target.removeEventListener("pointercancel", up as any);
      };
      target.addEventListener("pointermove", move as any);
      target.addEventListener("pointerup", up as any);
      target.addEventListener("pointercancel", up as any);
    },
    [width],
  );

  const onResizeDblClick = useCallback(() => {
    setWidth(DEFAULT_WIDTH_PX);
  }, []);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy) return;
    const s = settingsRef.current;
    const apiKey = activeApiKey(s);
    if (!apiKey) {
      setError(
        `No API key for ${s.provider}. Open Settings (⚙) and add one, then try again.`,
      );
      setView("settings");
      return;
    }
    const next: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setDraft("");
    setError(null);
    setStreaming(true);
    setItems([]);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat(
        backendOrigin,
        next,
        getSessionId(),
        {
          onDelta: (t) => {
            setItems(appendText(itemsRef.current, t));
          },
          onToolUseStart: (info) => {
            setItems(appendToolStart(itemsRef.current, info.id, info.name));
          },
          onToolUseEnd: (info) => {
            setItems(updateToolEnd(itemsRef.current, info));
          },
          onRich: (output) => {
            setItems(appendRich(itemsRef.current, output));
          },
          onDone: () => {
            const finalItems = itemsRef.current;
            const content = itemsToContent(finalItems);
            setMessages((m) => [
              ...m,
              { role: "assistant", content, items: finalItems },
            ]);
            setStreaming(false);
            setItems([]);
          },
          onError: (err) => {
            log.error("chat", err.message || "stream error", err);
            setError(err.message || "stream error");
            // Preserve whatever the assistant streamed before the error
            // (most commonly the iteration-limit cut-off). Mirrors stop():
            // finalise any still-running tool as halted, commit the partial
            // turn so it stays visible AND in the conversation context, and
            // only THEN clear the streaming buffer. Without this commit, the
            // next user turn loses the partial assistant work.
            const partialItems = itemsRef.current.map((it) =>
              it.kind === "tool" && it.status === "running"
                ? {
                    ...it,
                    status: "error" as const,
                    error_message: err.message || "stream error",
                  }
                : it,
            );
            if (partialItems.length) {
              const content = itemsToContent(partialItems);
              setMessages((m) => [
                ...m,
                { role: "assistant", content, items: partialItems },
              ]);
            }
            setStreaming(false);
            setItems([]);
          },
        },
        controller.signal,
        {
          provider: s.provider,
          apiKey,
          model: activeModel(s),
          maxTokens: s.maxTokens,
          maxToolIterations: s.maxToolIterations,
        },
      );
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        log.error("chat", e?.message || String(e), { stack: e?.stack });
        setError(e?.message || String(e));
      }
      setStreaming(false);
      setItems([]);
    } finally {
      abortRef.current = null;
    }
  }, [draft, busy, messages, backendOrigin]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    log.info("chat", "user stopped turn");
    if (streaming) {
      // Finalise in-flight tool entries so they don't keep showing the
      // pulsing "running" state after the user cancelled.
      const partialItems = itemsRef.current.map((it) =>
        it.kind === "tool" && it.status === "running"
          ? {
              ...it,
              status: "error" as const,
              error_message: "stopped by user",
            }
          : it,
      );
      const content = itemsToContent(partialItems);
      if (partialItems.length) {
        setMessages((m) => [
          ...m,
          { role: "assistant", content, items: partialItems },
        ]);
      }
      setStreaming(false);
      setItems([]);
    }
  }, [streaming]);

  const reset = useCallback(() => {
    if (busy) stop();
    setMessages([]);
    setError(null);
  }, [busy, stop]);

  const toggleView = useCallback((target: View) => {
    setView((cur) => (cur === target ? "chat" : target));
  }, []);

  const providerChip = PROVIDER_CHIP[settings.provider];
  const providerChipTitle = `Provider: ${settings.provider} · Model: ${activeModel(
    settings,
  )} · click to open Settings`;

  // When the chat drawer is collapsed, the report pane stretches to fill
  // the full viewport (drawerWidth = 0). Otherwise it leaves room for the
  // drawer on whichever edge the layout dictates.
  const drawerOffset = open ? width : 0;
  const layout = settings.layout ?? "chat-right";

  return (
    <div
      class="root"
      data-open={open ? "true" : "false"}
      data-resizing={resizing ? "true" : "false"}
      data-report={activeReport ? "true" : "false"}
      data-layout={layout}
      style={{ "--voitta-pane-width": width + "px" } as any}
    >
      {activeReport && (
        <ReportPane
          info={activeReport}
          onCollapse={() => setReportCollapsed(true)}
          collapsed={reportCollapsed}
          drawerWidth={drawerOffset}
          layout={layout}
        />
      )}
      {activeReport && reportCollapsed && (
        <button
          class="report-handle"
          type="button"
          title={
            activeReport.title
              ? `Reopen report: ${activeReport.title}`
              : "Reopen collapsed report"
          }
          aria-label="Reopen collapsed report"
          onClick={() => setReportCollapsed(false)}
        >
          {/* Document/report glyph — distinct from the chat handle's
              speech-bubble icon so the two are visually distinguishable
              when both are docked. */}
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
            <path
              d="M6 3h9l4 4v14H6z M14 3v5h5"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linejoin="round"
            />
          </svg>
        </button>
      )}
      {artifactsOpen && (
        <ArtifactsView
          backendOrigin={backendOrigin}
          onClose={() => setArtifactsOpen(false)}
        />
      )}
      <button
        class="handle"
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
            stroke-width="2"
            stroke-linejoin="round"
          />
        </svg>
      </button>

      <aside class="drawer" role="complementary" aria-label={brand.ariaLabel}>
        <div
          class="resizer"
          role="separator"
          aria-orientation="vertical"
          title="Drag to resize · double-click to reset"
          onPointerDown={onResizeDown as any}
          onDblClick={onResizeDblClick}
        />
        <header>
          {!hideBrand && (
            <span class="brand-mark" aria-hidden="true">
              ●
            </span>
          )}
          {!hideBrand && <span class="brand-name">{brand.name}</span>}
          <button
            class="provider-chip"
            type="button"
            title={providerChipTitle}
            onClick={() => toggleView("settings")}
          >
            {providerChip}
          </button>
          <span class="spacer" />
          {view === "chat" && (
            <button
              class="hbtn"
              type="button"
              title="Clear conversation"
              aria-label="Clear conversation"
              onClick={reset}
            >
              ↻
            </button>
          )}
          <button
            class={`hbtn ${artifactsOpen ? "active" : ""}`}
            type="button"
            title="Server artifacts"
            aria-label="Server artifacts"
            onClick={() => setArtifactsOpen((v) => !v)}
          >
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
              <path
                d="M3 6.5C3 5.67 3.67 5 4.5 5h4l2 2h9c.83 0 1.5.67 1.5 1.5V18c0 .83-.67 1.5-1.5 1.5h-15C3.67 19.5 3 18.83 3 18V6.5z"
                fill="none"
                stroke="currentColor"
                stroke-width="1.8"
                stroke-linejoin="round"
              />
            </svg>
          </button>
          <button
            class={`hbtn ${view === "settings" ? "active" : ""}`}
            type="button"
            title={view === "settings" ? "Back to chat" : "Settings"}
            aria-label={view === "settings" ? "Back to chat" : "Settings"}
            onClick={() => toggleView("settings")}
          >
            {view === "settings" ? "←" : "⚙"}
          </button>
          <button
            class={`hbtn ${view === "logs" ? "active" : ""}`}
            type="button"
            title={view === "logs" ? "Back to chat" : "Debug logs"}
            aria-label={view === "logs" ? "Back to chat" : "Debug logs"}
            onClick={() => toggleView("logs")}
          >
            {view === "logs" ? "←" : "≡"}
          </button>
          <button
            class="hbtn"
            type="button"
            title="Close"
            aria-label="Close"
            onClick={() => setOpen(false)}
          >
            ×
          </button>
        </header>
        {view === "chat" && needsAuth && (
          <InlineLogin
            backendOrigin={backendOrigin}
            onAuthenticated={() => setNeedsAuth(false)}
          />
        )}
        {view === "chat" && !needsAuth && authChecked && (
          <>
            <MessageList
              messages={messages}
              streamingItems={streamingItems}
              streaming={streaming}
              error={error}
            />
            <Composer
              value={draft}
              onChange={setDraft}
              onSend={send}
              onStop={stop}
              busy={busy}
            />
          </>
        )}
        {view === "settings" && <SettingsView backendOrigin={backendOrigin} />}
        {view === "logs" && <LogsView />}
      </aside>
    </div>
  );
}


// Inline login form rendered inside the drawer body when auth is
// required. Sits in place of MessageList/Composer; once the API key
// is accepted the parent flips ``needsAuth`` and the regular chat
// flow takes over. No new top-level pane, no separate router state.
function InlineLogin(props: {
  backendOrigin: string;
  onAuthenticated: () => void;
}) {
  const { backendOrigin, onAuthenticated } = props;
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await loginWithApiKey(backendOrigin, apiKey.trim());
    setBusy(false);
    if (r.ok) {
      setApiKey("");
      onAuthenticated();
    } else {
      setError(r.error || "Login failed.");
    }
  }

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        padding: "20px",
        overflowY: "auto",
      }}
    >
      <p class="muted" style={{ margin: 0 }}>
        This Voitta backend requires an API key.
      </p>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
        <label htmlFor="voitta-api-key" style={{ fontWeight: 600 }}>API key</label>
        <input
          id="voitta-api-key"
          type="password"
          value={apiKey}
          autoComplete="off"
          autoFocus
          disabled={busy}
          onInput={(e) => setApiKey((e.currentTarget as HTMLInputElement).value)}
          placeholder="enter key"
          style={{
            padding: "8px 10px",
            border: "1px solid var(--voitta-border)",
            borderRadius: "4px",
            background: "var(--voitta-surface)",
            color: "var(--voitta-text)",
            font: "inherit",
          }}
        />
        <button
          type="submit"
          class="save-btn"
          disabled={busy || !apiKey.trim()}
          style={{ marginTop: "4px" }}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error && (
        <div class="status err" role="alert" aria-live="polite">{error}</div>
      )}
    </div>
  );
}
