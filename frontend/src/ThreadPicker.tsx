// Conversation history dropdown — sits between the provider chip and
// the toolbar icons. Mode-aware:
//   • API providers (anthropic/openai/gemini) → Chainlit SQLite threads via
//     api.listThreads + Chainlit resume (unchanged).
//   • Claude (subscription) brain → the Claude Agent SDK's own session store
//     via /api/agent_sdk/sessions. Selecting one arms the next turn to resume
//     that session (continue-only) and starts a fresh Chainlit pane.

import { useCallback, useContext, useEffect, useState } from "react";
import {
  ChainlitContext,
  IThread,
  useChatMessages,
} from "@chainlit/react-client";
import { AGENT_SDK_PROVIDER } from "./lib/settings";
import { useSettings } from "./lib/useSettings";

interface SdkSession {
  session_id: string;
  title: string;
  summary?: string | null;
  first_prompt?: string | null;
  last_modified?: string | null;
}

function formatDate(raw: string | number | undefined | null): string {
  if (!raw) return "";
  const d = new Date(typeof raw === "number" ? raw : raw);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / 86_400_000);
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function threadLabel(t: IThread): string {
  return t.name?.trim() || formatDate(t.createdAt) || t.id.slice(0, 8);
}

function sdkLabel(s: SdkSession): string {
  return (
    s.title?.trim() ||
    s.summary?.trim() ||
    s.first_prompt?.trim() ||
    formatDate(s.last_modified) ||
    s.session_id.slice(0, 8)
  );
}

interface Props {
  backendOrigin: string;
  selectedThreadId: string | null;
  onThreadSelect: (threadId: string | null) => void;
}

export default function ThreadPicker({ backendOrigin, selectedThreadId, onThreadSelect }: Props) {
  const api = useContext(ChainlitContext);
  const { threadId: currentThreadId } = useChatMessages();
  const settings = useSettings();
  const brainMode = settings.provider === AGENT_SDK_PROVIDER;

  const [open, setOpen] = useState(false);
  const [threads, setThreads] = useState<IThread[]>([]);
  const [sdkSessions, setSdkSessions] = useState<SdkSession[]>([]);
  const [activeSdkId, setActiveSdkId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchThreads = useCallback(async () => {
    if (!api) return;
    setLoading(true);
    try {
      const res = await api.listThreads({ first: 50 }, {});
      const sorted = [...(res.data ?? [])].sort((a, b) => {
        const ta = new Date(a.createdAt).getTime();
        const tb = new Date(b.createdAt).getTime();
        return tb - ta;
      });
      setThreads(sorted);
    } catch (err) {
      console.warn("[ThreadPicker] fetchThreads error", err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  const fetchSdkSessions = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${backendOrigin}/api/agent_sdk/sessions`, {
        credentials: "include",
      });
      const body = (await res.json()) as { sessions?: SdkSession[] };
      setSdkSessions(body.sessions ?? []);
    } catch (err) {
      console.warn("[ThreadPicker] fetchSdkSessions error", err);
      setSdkSessions([]);
    } finally {
      setLoading(false);
    }
  }, [backendOrigin]);

  const refresh = useCallback(() => {
    if (brainMode) fetchSdkSessions();
    else fetchThreads();
  }, [brainMode, fetchSdkSessions, fetchThreads]);

  useEffect(() => {
    refresh();
  }, [currentThreadId, refresh]);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  // Switch an API-provider thread (Chainlit resume).
  const switchThread = useCallback(
    (threadId: string | null) => {
      setOpen(false);
      onThreadSelect(threadId);
    },
    [onThreadSelect],
  );

  // Arm the next turn to resume an SDK session (or start fresh), then reset the
  // Chainlit pane to a clean conversation. The resumed context lives in the SDK
  // session server-side; new turns continue it.
  const switchSdk = useCallback(
    async (sessionId: string | null) => {
      setOpen(false);
      setActiveSdkId(sessionId);
      try {
        await fetch(`${backendOrigin}/api/agent_sdk/select`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify(sessionId ? { session_id: sessionId } : { new: true }),
        });
      } catch (err) {
        console.warn("[ThreadPicker] select error", err);
      }
      onThreadSelect(null); // fresh Chainlit pane
    },
    [backendOrigin, onThreadSelect],
  );

  // Label: confirmed Chainlit thread (API mode) or active SDK session.
  const activeId = currentThreadId ?? selectedThreadId;
  let label = "conversations";
  if (brainMode) {
    const active = sdkSessions.find((s) => s.session_id === activeSdkId);
    label = active ? sdkLabel(active) : "Claude sessions";
  } else {
    const currentThread = threads.find((t) => t.id === activeId);
    if (currentThread) label = threadLabel(currentThread);
  }

  return (
    <div className="thread-picker">
      <button
        className="thread-picker-btn"
        type="button"
        title="Conversation history"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="thread-picker-label">{label}</span>
        <svg className="thread-picker-caret" viewBox="0 0 8 5" width="8" height="5" aria-hidden="true">
          <path d="M0 0l4 5 4-5z" fill="currentColor" />
        </svg>
      </button>

      {open && (
        <>
          <div className="thread-picker-backdrop" onClick={() => setOpen(false)} />

          <div className="thread-picker-menu" role="listbox">
            <button
              className={`thread-picker-item ${(brainMode ? !activeSdkId : !activeId) ? "active" : ""}`}
              role="option"
              aria-selected={brainMode ? !activeSdkId : !activeId}
              type="button"
              onClick={() => (brainMode ? switchSdk(null) : switchThread(null))}
            >
              <span className="thread-picker-item-name">+ New conversation</span>
            </button>

            {loading && <div className="thread-picker-loading">loading…</div>}

            {!loading && brainMode && sdkSessions.length === 0 && (
              <div className="thread-picker-empty">No Claude sessions yet</div>
            )}
            {!loading && !brainMode && threads.length === 0 && (
              <div className="thread-picker-empty">No conversations yet</div>
            )}

            {!loading && brainMode &&
              sdkSessions.map((s) => (
                <button
                  key={s.session_id}
                  className={`thread-picker-item ${s.session_id === activeSdkId ? "active" : ""}`}
                  role="option"
                  aria-selected={s.session_id === activeSdkId}
                  type="button"
                  onClick={() => switchSdk(s.session_id)}
                >
                  <span className="thread-picker-item-name">{sdkLabel(s)}</span>
                  <span className="thread-picker-item-date">{formatDate(s.last_modified)}</span>
                </button>
              ))}

            {!loading && !brainMode &&
              threads.map((t) => (
                <button
                  key={t.id}
                  className={`thread-picker-item ${t.id === activeId ? "active" : ""}`}
                  role="option"
                  aria-selected={t.id === activeId}
                  type="button"
                  onClick={() => switchThread(t.id)}
                >
                  <span className="thread-picker-item-name">{threadLabel(t)}</span>
                  <span className="thread-picker-item-date">{formatDate(t.createdAt)}</span>
                </button>
              ))}
          </div>
        </>
      )}
    </div>
  );
}
