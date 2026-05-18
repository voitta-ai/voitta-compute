// Chat-stream client: POST /api/chat/stream and read the SSE response.
//
// We parse `event:` / `data:` blocks ourselves so the request method can stay
// POST (EventSource is GET-only).

import type { ImageAttachment } from "./image-attach";
import type { RichOutput } from "./plot-spec";
import type { ProviderId } from "./settings";

export type Role = "user" | "assistant";

export type { ProviderId };
export type { ImageAttachment };

// One sequential element of an assistant turn. The list is built in stream
// order so re-rendering it preserves the original interleaving of text,
// tool-call entries, and rich (plot/text/log) outputs emitted by browser-
// side primitives like buffer_eval.
export type TurnItem =
  | { kind: "text"; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      status: "running" | "ok" | "error";
      latency_ms?: number;
      error_message?: string | null;
      input?: unknown;
      result_preview?: string | null;
      // Cumulative char count of tool-args fragments received so far,
      // updated on every `tool_args_delta` SSE event. Cleared once the
      // tool finishes (status leaves "running").
      args_chars?: number;
    }
  | { kind: "rich"; output: RichOutput };

export interface ChatMessage {
  role: Role;
  // What gets sent to the backend. For user turns it's just the prompt; for
  // assistant turns it's the concatenation of text-item contents (the backend
  // ignores everything else in a fresh request, so this stays accurate
  // without redoing the orchestrator's history shape).
  content: string;
  // User-attached images. When present, the wire shape switches from a plain
  // string to an Anthropic-style blocks list (see toWireMessage below).
  // Persists across turns — every subsequent /api/chat/stream POST re-sends
  // them as part of conversation history.
  attachments?: ImageAttachment[];
  // Frontend-only: the rich, ordered representation used for rendering.
  // Optional so back-loaded transcripts without items still render as plain
  // text.
  items?: TurnItem[];
  // Per-iteration wire blocks captured from the backend's `turn_persist`
  // SSE events. An agent loop with N iterations produces N entries; the
  // LAST entry may have an empty toolResultBlocks (final text-only
  // assistant message that ended the loop).
  //
  // Anthropic requires every tool_use block to be IMMEDIATELY followed
  // by its tool_result in the next message. So we must preserve the
  // iteration boundaries: each TurnPair becomes one (assistant, user)
  // pair on the wire. Flattening across iterations is wrong — it
  // produces messages with tool_use blocks that have no matching
  // tool_result, which Anthropic 400s. See messagesToWire below.
  //
  // Without this, the model loses memory of its tool use after one
  // turn and starts "pretending" to make edits — that's the bug this
  // whole capture/replay system fixes.
  turnPairs?: TurnPair[];
}

// One iteration of the backend agent loop. The assistant blocks are
// what the model emitted (text + tool_use entries); the tool result
// blocks are what the orchestrator appended after dispatch (one
// tool_result per tool_use, possibly prefixed by a system-reminder
// text block from RenderDrain).
export interface TurnPair {
  assistantBlocks: WireBlock[];
  toolResultBlocks: WireBlock[];
}

// Loose pass-through type — Anthropic blocks of various shapes. We
// don't validate; the backend is the source of truth and the FE just
// stores and replays.
export type WireBlock = Record<string, unknown>;

export interface ToolUseStart {
  id: string;
  name: string;
}

export interface ToolArgsDelta {
  id: string;
  block_index: number;
  chars: number;
}

export interface ToolUseEnd {
  id: string;
  name?: string;
  ok: boolean;
  latency_ms?: number;
  error?: { kind?: string; message?: string } | null;
  input?: unknown;
  result_preview?: string | null;
}

export interface TurnPersist {
  iteration: number;
  /** Anthropic-style blocks (text + tool_use) the model produced this iteration. */
  assistant_blocks: WireBlock[];
  /** Anthropic-style blocks for the synthetic `{role:"user"}` follow-up
   *  message the backend appended (tool_result entries, plus an optional
   *  leading text block carrying a system-reminder from RenderDrain). */
  tool_result_blocks: WireBlock[];
}

export interface StreamCallbacks {
  onStart?: (info: { model: string; provider?: string; tools?: string[] }) => void;
  onDelta: (text: string) => void;
  onToolUseStart?: (info: ToolUseStart) => void;
  onToolArgsDelta?: (info: ToolArgsDelta) => void;
  onToolUseEnd?: (info: ToolUseEnd) => void;
  /** Per-iteration replayable wire blocks. Store these on the in-progress
   * assistant ChatMessage so subsequent POSTs include the full tool-use
   * history. Fires AFTER tool_use_end for the same iteration. */
  onTurnPersist?: (info: TurnPersist) => void;
  /** Server-side rich output emitted by tools that produce inline blocks
   * (e.g. `run_compute`'s `ctx.text` / `ctx.image`). Frontend appends to
   * the streaming TurnItems. Same renderer as browser-side rich items
   * from buffer_eval / plot primitives. */
  onRich?: (output: import("./plot-spec").RichOutput) => void;
  onDone?: (info: {
    stop_reason: string | null;
    usage: unknown;
    iterations?: number;
  }) => void;
  onError?: (err: { message: string; type?: string }) => void;
}

export interface StreamOptions {
  /** Provider chosen in the Settings panel. Required — server has no default. */
  provider: ProviderId;
  /** Provider API key from the Settings panel. */
  apiKey: string;
  /** Model id chosen in the Settings panel. */
  model: string;
  /** Max tokens per iteration. */
  maxTokens?: number;
  /** Max tool-use iterations per turn. */
  maxToolIterations?: number;
}

// Prefix the LATEST user message with the current SPA URL so the model can
// always tell what page the user is looking at. the original plugin's content.js
// uses the same convention; without it, the model loses track on SPA
// navigations.
function prefixCurrentUrl(messages: ChatMessage[]): ChatMessage[] {
  if (!messages.length) return messages;
  const last = messages[messages.length - 1];
  if (last.role !== "user") return messages;
  const prefix = `(current url: ${location.href})\n\n`;
  if (last.content.startsWith(prefix.slice(0, 14))) return messages; // already tagged
  const tagged: ChatMessage = { ...last, content: prefix + last.content };
  return [...messages.slice(0, -1), tagged];
}

// Flatten a list of ChatMessage into the wire shape.
//
// Most turns are 1:1 with a wire entry. The exception is an assistant
// turn that drove an agent loop: it carries `turnPairs`, an ordered
// list of (assistantBlocks, toolResultBlocks) iterations. Each pair
// becomes a separate (assistant, user) wire entry — Anthropic
// requires every `tool_use` block to be immediately followed by its
// matching `tool_result` in the next message, so we cannot flatten
// multiple iterations into a single assistant block list. The last
// pair may have an empty `toolResultBlocks` (final text-only
// completion that ended the loop); that pair becomes a single
// assistant entry, no follow-up user entry.
//
// Plain text-only turns and image-bearing user turns keep the
// historical behaviour (string content, or text+image blocks).
function messagesToWire(
  msgs: ChatMessage[],
): Array<{ role: Role; content: unknown }> {
  const out: Array<{ role: Role; content: unknown }> = [];
  for (const m of msgs) {
    if (m.role === "assistant" && m.turnPairs && m.turnPairs.length > 0) {
      for (const pair of m.turnPairs) {
        // Defensive: drop orphan tool_use blocks from a pair whose
        // toolResultBlocks is empty. Without this, an old/replayed
        // pair carrying a partial tool_use (max_tokens cutoff, etc.)
        // sends an assistant turn with tool_use that has no following
        // tool_result, which Anthropic 400s on. Belt-and-braces with
        // the BE-side strip in routes/chat.py.
        const safeAssistant =
          pair.toolResultBlocks.length === 0
            ? pair.assistantBlocks.filter(
                (b) => (b as { type?: string }).type !== "tool_use",
              )
            : pair.assistantBlocks;
        if (safeAssistant.length > 0) {
          out.push({ role: "assistant", content: safeAssistant });
        }
        if (pair.toolResultBlocks.length > 0) {
          out.push({ role: "user", content: pair.toolResultBlocks });
        }
      }
      continue;
    }
    if (!m.attachments || m.attachments.length === 0) {
      out.push({ role: m.role, content: m.content });
      continue;
    }
    const blocks: unknown[] = m.attachments.map((a) => ({
      type: "image",
      source: { type: "base64", media_type: a.mime, data: a.data },
    }));
    blocks.push({ type: "text", text: m.content });
    out.push({ role: m.role, content: blocks });
  }
  return out;
}

export async function streamChat(
  backendOrigin: string,
  messages: ChatMessage[],
  sessionId: string,
  cb: StreamCallbacks,
  signal: AbortSignal | undefined,
  options: StreamOptions,
): Promise<void> {
  // Strip the frontend-only `items` field; backend only looks at
  // {role, content}. Inject the URL prefix on the latest user turn,
  // then combine text + attachments into the wire shape.
  const wirePayload = messagesToWire(prefixCurrentUrl(messages));

  const body: Record<string, unknown> = {
    messages: wirePayload,
    session_id: sessionId,
    provider: options.provider,
    api_key: options.apiKey,
    model: options.model,
  };
  if (options.maxTokens != null) body.max_tokens = options.maxTokens;
  if (options.maxToolIterations != null) body.max_tool_iterations = options.maxToolIterations;

  const res = await fetch(`${backendOrigin}/api/chat/stream`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
    credentials: "include",
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    cb.onError?.({ message: `HTTP ${res.status}: ${text || res.statusText}` });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE separates events with a blank line. sse-starlette emits CRLF; some
    // proxies normalise to LF. Handle both.
    let m = boundary(buf);
    while (m) {
      const block = buf.slice(0, m.index);
      buf = buf.slice(m.index + m.length);
      handleBlock(block, cb);
      m = boundary(buf);
    }
  }
  if (buf.trim()) handleBlock(buf, cb);
}

function boundary(s: string): { index: number; length: number } | null {
  const a = s.indexOf("\r\n\r\n");
  const b = s.indexOf("\n\n");
  if (a === -1 && b === -1) return null;
  if (a === -1) return { index: b, length: 2 };
  if (b === -1) return { index: a, length: 4 };
  return a < b ? { index: a, length: 4 } : { index: b, length: 2 };
}

function handleBlock(block: string, cb: StreamCallbacks): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  let parsed: any;
  try {
    parsed = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  switch (event) {
    case "start":
      cb.onStart?.(parsed);
      break;
    case "delta":
      if (typeof parsed?.text === "string") cb.onDelta(parsed.text);
      break;
    case "tool_use_start":
      cb.onToolUseStart?.(parsed);
      break;
    case "tool_args_delta":
      cb.onToolArgsDelta?.(parsed);
      break;
    case "tool_use_end":
      cb.onToolUseEnd?.(parsed);
      break;
    case "turn_persist":
      cb.onTurnPersist?.(parsed);
      break;
    case "rich":
      cb.onRich?.(parsed);
      break;
    case "done":
      cb.onDone?.(parsed);
      break;
    case "error":
      cb.onError?.(parsed);
      break;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Provider model listing.
//
// POST /api/providers/models with the chosen provider + API key and the
// backend pulls the catalog from the provider's SDK. Used by SettingsView
// to populate the model dropdown so we don't have to hand-maintain the
// list in MODELS_BY_PROVIDER.

export interface ProviderModel {
  id: string;
  display_name?: string | null;
}

export async function fetchProviderModels(
  backendOrigin: string,
  provider: ProviderId,
  apiKey: string,
  signal?: AbortSignal,
): Promise<ProviderModel[]> {
  const res = await fetch(`${backendOrigin}/api/providers/models`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ provider, api_key: apiKey }),
    signal,
    credentials: "include",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  const data = (await res.json()) as { models?: ProviderModel[] };
  return data.models ?? [];
}

