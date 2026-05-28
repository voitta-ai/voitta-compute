// Conversation history dropdown — sits between the provider chip and
// the toolbar icons. Fetches threads on open, lets the user switch.

import { useCallback, useContext, useEffect, useState } from "react";
import {
  ChainlitContext,
  IThread,
  useChatMessages,
} from "@chainlit/react-client";

function formatDate(raw: string | number | undefined): string {
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

interface Props {
  backendOrigin: string;
  selectedThreadId: string | null;
  onThreadSelect: (threadId: string | null) => void;
}

export default function ThreadPicker({ backendOrigin: _backendOrigin, selectedThreadId, onThreadSelect }: Props) {
  const api = useContext(ChainlitContext);
  const { threadId: currentThreadId } = useChatMessages();

  const [open, setOpen] = useState(false);
  const [threads, setThreads] = useState<IThread[]>([]);
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
    } catch(err) {
      console.warn("[ThreadPicker] fetchThreads error", err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchThreads();
  }, [currentThreadId, fetchThreads]);

  useEffect(() => {
    if (open) fetchThreads();
  }, [open, fetchThreads]);

  const switchTo = useCallback(
    (threadId: string | null) => {
      setOpen(false);
      onThreadSelect(threadId);
    },
    [onThreadSelect],
  );

  // Displayed label: prefer the confirmed currentThreadId from the socket,
  // fall back to selectedThreadId while connecting.
  const activeId = currentThreadId ?? selectedThreadId;
  const currentThread = threads.find((t) => t.id === activeId);
  const label = currentThread ? threadLabel(currentThread) : "conversations";

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
          {/* Full-screen backdrop rendered inside the shadow root.
              document-level mousedown listeners can't see inside shadow DOM,
              so we use this instead of a click-outside handler. */}
          <div className="thread-picker-backdrop" onClick={() => setOpen(false)} />

          <div className="thread-picker-menu" role="listbox">
            {/* New conversation */}
            <button
              className={`thread-picker-item ${!activeId ? "active" : ""}`}
              role="option"
              aria-selected={!activeId}
              type="button"
              onClick={() => switchTo(null)}
            >
              <span className="thread-picker-item-name">+ New conversation</span>
            </button>

            {loading && (
              <div className="thread-picker-loading">loading…</div>
            )}

            {!loading && threads.length === 0 && (
              <div className="thread-picker-empty">No conversations yet</div>
            )}

            {!loading && threads.map((t) => (
              <button
                key={t.id}
                className={`thread-picker-item ${t.id === activeId ? "active" : ""}`}
                role="option"
                aria-selected={t.id === activeId}
                type="button"
                onClick={() => switchTo(t.id)}
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
