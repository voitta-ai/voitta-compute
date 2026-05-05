import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { streamChat, type ChatMessage, type TurnItem } from "../lib/api";
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

const BRAND = { name: "Voitta", ariaLabel: "Voitta chat pane" };
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

export function ChatPane({ backendOrigin }: Props) {
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
  const [artifactsOpen, setArtifactsOpen] = useState(false);
  const brand = BRAND;
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
    setReportSink((info) => setActiveReport(info));
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

      const move = (ev: PointerEvent) => {
        setWidth(clampWidth(startW - (ev.clientX - startX)));
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

  // When the chat drawer is collapsed, the report pane stretches to the
  // right edge (drawerWidth = 0). Otherwise it leaves room for the drawer.
  const reportRightOffset = open ? width : 0;

  return (
    <div
      class="root"
      data-open={open ? "true" : "false"}
      data-resizing={resizing ? "true" : "false"}
      data-report={activeReport ? "true" : "false"}
      style={{ "--voitta-pane-width": width + "px" } as any}
    >
      {activeReport && (
        <ReportPane
          info={activeReport}
          onClose={() => setActiveReport(null)}
          drawerWidth={reportRightOffset}
        />
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
          <span class="brand-mark" aria-hidden="true">
            ●
          </span>
          <span class="brand-name">{brand.name}</span>
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
        {view === "chat" && (
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
