import { useEffect, useRef, useState } from "preact/hooks";
import type { ChatMessage, TurnItem } from "../lib/api";
import { getBackendOrigin } from "../lib/bridge";
import type { RichOutput } from "../lib/plot-spec";
import { SPINNER_VERBS } from "../lib/spinnerVerbs";
import { Markdown } from "./Markdown";
import { PlotCard } from "./PlotCard";

// Rotating "thinking" indicator. Picks a random Claude-Code-style verb on
// mount and rotates with a fresh delay drawn uniformly from [1000, 2000]
// ms each tick (~0.5–1 Hz). setTimeout (not setInterval) so the delay
// can be re-randomised on every cycle. Local state only — parent
// re-renders (e.g. each new tool-stream item) don't reset the cycle.
function SpinnerVerb() {
  const [verb, setVerb] = useState(
    () => SPINNER_VERBS[Math.floor(Math.random() * SPINNER_VERBS.length)],
  );
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      setVerb(SPINNER_VERBS[Math.floor(Math.random() * SPINNER_VERBS.length)]);
      const delay = 1000 + Math.random() * 1000;
      timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, 1000 + Math.random() * 1000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);
  return (
    <span class="spinner-verb">
      {verb}
      <span class="dots">…</span>
    </span>
  );
}

interface Props {
  messages: ChatMessage[];
  streamingItems: TurnItem[];
  streaming: boolean;
  error: string | null;
}

function ToolBlock({ item }: { item: Extract<TurnItem, { kind: "tool" }> }) {
  const symbol = item.status === "ok" ? "✓" : item.status === "error" ? "✗" : "…";
  const headExtra =
    item.status === "ok" && item.latency_ms != null
      ? ` · ${item.latency_ms} ms`
      : item.status === "error"
        ? ` · ${item.error_message || "failed"}`
        : "";
  const inputText =
    item.input !== undefined
      ? typeof item.input === "string"
        ? item.input
        : JSON.stringify(item.input, null, 2)
      : "";
  const resultText = item.result_preview ?? "";
  const expandable = item.status !== "running" && (inputText || resultText);

  if (!expandable) {
    return (
      <div class={`tool-line status-${item.status}`}>
        <span class="sym">{symbol}</span>
        <span class="name">{item.name}</span>
        {headExtra && <span class="extra">{headExtra}</span>}
      </div>
    );
  }
  return (
    <details class={`tool-line status-${item.status}`}>
      <summary>
        <span class="sym">{symbol}</span>
        <span class="name">{item.name}</span>
        {headExtra && <span class="extra">{headExtra}</span>}
      </summary>
      <pre class="tool-body">
        {inputText && `input:\n${inputText}\n\n`}
        {`result:\n${resultText || "(empty)"}`}
      </pre>
    </details>
  );
}

function resolveBackendUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  const origin = getBackendOrigin();
  if (!origin) return url;
  return origin.replace(/\/$/, "") + (url.startsWith("/") ? url : "/" + url);
}

function RichBlock({ output }: { output: RichOutput }) {
  if (output.kind === "plot" && output.plot) {
    return <PlotCard spec={output.plot} />;
  }
  if (output.kind === "text" && typeof output.markdown === "string") {
    return (
      <div class="rich-text">
        <Markdown text={output.markdown} />
      </div>
    );
  }
  if (output.kind === "image" && typeof output.url === "string") {
    return (
      <figure class="rich-image">
        <img src={resolveBackendUrl(output.url)} alt={output.alt || ""} />
        {output.alt ? <figcaption>{output.alt}</figcaption> : null}
      </figure>
    );
  }
  if (output.kind === "log" && output.log_lines && output.log_lines.length) {
    return (
      <details class="rich-log">
        <summary>
          {output.log_lines.length} log line
          {output.log_lines.length === 1 ? "" : "s"}
        </summary>
        <pre>{output.log_lines.join("\n")}</pre>
      </details>
    );
  }
  return null;
}

function ItemView({ item }: { item: TurnItem }) {
  if (item.kind === "text") {
    if (!item.text) return null;
    return (
      <div class="msg assistant">
        <Markdown text={item.text} />
      </div>
    );
  }
  if (item.kind === "tool") {
    return <ToolBlock item={item} />;
  }
  return <RichBlock output={item.output} />;
}

function AssistantTurn({ items }: { items: TurnItem[] }) {
  if (!items.length) return null;
  return (
    <div class="turn assistant">
      {items.map((it, i) => (
        <ItemView key={i} item={it} />
      ))}
    </div>
  );
}

export function MessageList({ messages, streamingItems, streaming, error }: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streamingItems, streaming, error]);

  const empty = messages.length === 0 && !streaming && !error;

  return (
    <div class="messages" ref={listRef}>
      {empty && (
        <div class="empty">
          <div class="badge">💬</div>
          <div>Ask me anything — try Drive search, a web fetch, or build a report.</div>
        </div>
      )}
      {messages.map((m, i) => {
        if (m.role === "user") {
          // Strip the auto-prefixed "(current url: ...)" tag from the rendered
          // copy so the user doesn't see their own message decorated with it.
          const display = m.content.replace(/^\(current url: [^)]*\)\n\n/, "");
          return (
            <div key={i} class="msg user">
              {display}
            </div>
          );
        }
        if (m.items && m.items.length) {
          return <AssistantTurn key={i} items={m.items} />;
        }
        // Legacy / pre-items assistant message: just text.
        return (
          <div key={i} class="msg assistant">
            <Markdown text={m.content} />
          </div>
        );
      })}
      {streaming && (
        <div class="turn assistant streaming">
          {streamingItems.map((it, i) => (
            <ItemView key={`s${i}`} item={it} />
          ))}
          {/* Always show the rotating verb while streaming, even after
              tool calls have started filling the turn — the user
              should never wonder whether things are still moving. */}
          <SpinnerVerb />
        </div>
      )}
      {error && <div class="msg error">{error}</div>}
    </div>
  );
}
