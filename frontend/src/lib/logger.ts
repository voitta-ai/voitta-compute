// Browser-side logger. In-memory ring buffer + subscribe + dump + console
// mirror.
//
// All bridge / primitive / chat-pane code that wants to surface a recoverable
// event (error, warning, interesting info) calls log.error / log.warn /
// log.info. The LogsView component subscribes and re-renders on every entry.
//
// We deliberately also call console.{log,warn,error} so DevTools shows the
// same data — but most users won't have DevTools open, hence the in-pane view.

export type LogLevel = "info" | "warn" | "error";

export interface LogEntry {
  ts: string; // ISO 8601
  level: LogLevel;
  tag: string;
  msg: string;
  data?: unknown;
}

const MAX = 2000;
const buffer: LogEntry[] = [];
const listeners = new Set<(e: LogEntry) => void>();

function record(level: LogLevel, tag: string, msg: string, data?: unknown): void {
  const entry: LogEntry = { ts: new Date().toISOString(), level, tag, msg, data };
  buffer.push(entry);
  if (buffer.length > MAX) buffer.shift();
  const line = `[${entry.ts}] [${level}] [${tag}] ${msg}`;
  if (level === "error") console.error(line, data ?? "");
  else if (level === "warn") console.warn(line, data ?? "");
  else console.log(line, data ?? "");
  for (const fn of listeners) {
    try {
      fn(entry);
    } catch {
      /* listener errors don't kill the producer */
    }
  }
}

export const log = {
  info: (tag: string, msg: string, data?: unknown) => record("info", tag, msg, data),
  warn: (tag: string, msg: string, data?: unknown) => record("warn", tag, msg, data),
  error: (tag: string, msg: string, data?: unknown) => record("error", tag, msg, data),
};

export function subscribe(fn: (e: LogEntry) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function snapshot(): LogEntry[] {
  return buffer.slice();
}

export function dump(): string {
  return buffer
    .map((e) => {
      const head = `[${e.ts}] [${e.level}] [${e.tag}] ${e.msg}`;
      if (e.data === undefined) return head;
      let body: string;
      try {
        body = typeof e.data === "string" ? e.data : JSON.stringify(e.data, null, 2);
      } catch {
        body = String(e.data);
      }
      return `${head}\n${body}`;
    })
    .join("\n");
}

export function clearLogs(): void {
  buffer.length = 0;
  // Sentinel entry so subscribers can update their UI.
  for (const fn of listeners) {
    try {
      fn({ ts: "", level: "info", tag: "", msg: "__clear__" });
    } catch {
      /* ignore */
    }
  }
}

declare global {
  interface Window {
    VoittaBookmarklet?: {
      mount?: () => void;
      log?: typeof log;
      logSnapshot?: typeof snapshot;
    };
  }
}
if (typeof window !== "undefined") {
  window.VoittaBookmarklet = { ...(window.VoittaBookmarklet || {}), log, logSnapshot: snapshot };
}
