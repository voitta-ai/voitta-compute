// In-pane log viewer.
//
// Subscribes to the global ring buffer in lib/logger.ts. Auto-scrolls to
// bottom on new entries. The "Copy" button drops a `dump()` text snapshot
// onto the clipboard.

import { useEffect, useRef, useState } from "preact/hooks";
import { clearLogs, dump, type LogEntry, snapshot, subscribe } from "../lib/logger";

function formatBody(data: unknown): string {
  if (data === undefined) return "";
  try {
    return typeof data === "string" ? data : JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

export function LogsView() {
  const [entries, setEntries] = useState<LogEntry[]>(() => snapshot());
  const [copied, setCopied] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const off = subscribe((entry) => {
      if (entry.msg === "__clear__") {
        setEntries([]);
        return;
      }
      setEntries((prev) => {
        const next = [...prev, entry];
        return next.length > 2000 ? next.slice(-2000) : next;
      });
    });
    return off;
  }, []);

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries]);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(dump());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  }

  return (
    <section class="view view-logs">
      <div class="logs-toolbar">
        <span class="logs-title">Logs</span>
        <span class="logs-count">{entries.length}</span>
        <span class="spacer" />
        <button type="button" onClick={onCopy} title="Copy all log lines">
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" onClick={clearLogs} title="Clear log buffer">
          Clear
        </button>
      </div>
      <div class="logs-body" ref={bodyRef}>
        {entries.length === 0 ? (
          <div class="empty">No log entries yet.</div>
        ) : (
          entries.map((e, i) => {
            const body = formatBody(e.data);
            const head = `[${e.ts.replace("T", " ").replace(/\.\d+Z$/, "Z")}] [${e.tag}] ${e.msg}`;
            return (
              <div key={i} class={`log-line log-${e.level}`}>
                <span class="head">{head}</span>
                {body && <pre class="body">{body}</pre>}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
