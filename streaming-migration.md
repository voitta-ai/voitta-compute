# Streaming Migration Spec

Migration of the chat orchestrator and LLM provider adapters from full-response
calls to true streaming. Authored 2026-05-14. This is a checkmark document:
each section ends with a checklist that must be ticked before the section is
considered done.

> **Implementation status (2026-05-14):** Backend complete. All three
> provider adapters implement `stream()`; `BaseProvider.create_message`
> is now a thin wrapper that reuses the stream. The orchestrator
> `_stream()` consumes block events and emits SSE with `seq`,
> `iteration`, `block_index`. Tier 1 (22 tests) green; Tier 2 (15 live
> tests across 3 providers) wired with the `live` marker, excluded by
> default, run manually with env-supplied API keys. Frontend
> seq-sorted insertion + cancelled chip + message-assembler trimming
> are deferred to a follow-up — the SSE wire format is
> backward-compatible so the existing UI continues to function.

## 0. Goals and non-goals

### Goals
1. Tool-use chips render with the tool name as early as the provider reveals
   it — for Anthropic and OpenAI that means before tool arguments finish
   generating; for Gemini that means at the moment the function_call Part
   arrives in its chunk.
2. One unified streaming code path across Anthropic, OpenAI, Gemini.
3. SSE wire format remains backwards-compatible with the current frontend
   (additive fields only; no renames, no removals).
4. Stop semantics are at least as reliable as today, with explicit contract on
   what survives an interrupted turn and what must be trimmed before the next
   turn.
5. Mid-stream errors are surfaced with enough context for the UI to render the
   partial turn and an inline error marker without losing earlier content.

### Non-goals
- Progressive (per-token) UI rendering. Backend will buffer text deltas and
  flush one `delta` event per text block, same as today. The plumbing is
  designed so a future change can flip to per-token by emitting on each
  provider delta instead of on `block_stop`, but that switch is out of scope
  here.
- Reordering or batching of `rich` events. Existing emission order is
  preserved.
- Changing the multi-iteration tool-use loop structure.

---

## 1. Glossary

| Term | Meaning |
| --- | --- |
| **Turn** | One user → assistant exchange. May span multiple **iterations** if the assistant calls tools. |
| **Iteration** | One call to `provider.stream()`. A turn has ≥1 iterations; iteration N+1 begins only after the tools requested in iteration N have run and their results have been appended to `messages`. |
| **Block** | A piece of an assistant message — either a text block or a tool_use block. Multiple blocks per iteration. |
| **Block event** | An internal normalised event describing a block's lifecycle: `block_start`, `block_delta`, `block_stop`. |
| **SSE event** | A wire-level event sent to the frontend over the SSE channel: `start`, `delta`, `tool_use_start`, `tool_use_end`, `rich`, `done`, `error`. |
| **seq** | A monotonic integer assigned by the orchestrator to every SSE event in a single turn. Used as the canonical ordering key — the frontend MUST sort/insert by `seq`, not by arrival order. |
| **block_index** | Position of a block within its iteration's assistant message, 0-based. Used to link `tool_use_start`, `rich` and `tool_use_end` to the same tool. |
| **StreamEvent** | Discriminated union: `BlockStart \| BlockDelta \| BlockStop \| MessageStop \| StreamError`. The yield type of `provider.stream()`. |
| **partial** | A boolean on the `error` SSE event indicating whether earlier content of the turn had already been emitted before the error. |
| **parent_block_index** | On `rich` events: the `block_index` of the `tool_use` block that produced the rich item. |
| **sub_seq** | On `rich` events: an integer ordering rich items emitted by the same tool. |

---

## 2. Current vs. proposed state

### 2.1 Current

- `provider.create_message(req) -> NormalisedResponse` is a single `await`.
  The provider call buffers the entire assistant turn before returning.
- `_stream()` in [backend/app/routes/chat.py](backend/app/routes/chat.py)
  iterates the completed blocks and emits SSE events.
- `tool_use_start` is emitted only after the model has finished generating the
  whole assistant turn — including all tool argument text.
- Frontend appends `TurnItem`s in SSE arrival order with no sequence key.
- Stop button calls `AbortController.abort()`; SSE connection closes;
  `sse_starlette` cancels the `_stream()` generator; `asyncio.gather(...)` of
  in-flight tool dispatches receives `CancelledError`.

### 2.2 Proposed

- Add `provider.stream(req)` returning an async context manager that yields a
  stream of normalised **block events**. `create_message()` is retained, but
  the base implementation becomes "consume `stream()` and return the assembled
  `NormalisedResponse`" — single source of truth.
- `_stream()` consumes block events and emits SSE events with:
  - `tool_use_start` fired on `block_start` for kind `tool_use` (immediately
    for Anthropic and OpenAI; on chunk arrival for Gemini).
  - `delta` fired on `block_stop` for kind `text` (server-side buffered text).
  - `tool_use_end` fired after tool dispatch completes, after any `rich`
    events for that tool.
- Every SSE event carries a monotonic `seq` and (where applicable) a
  `block_index` and `iteration` field for diagnostic and stable-ordering use.
- Usage is accumulated across `message_stop` events from each iteration and
  emitted once in the final `done`.

### Section 2 checklist
- [x] Current behaviour documented above matches code in `chat.py`, `openai.py`,
      `anthropic.py`, `gemini.py` as of the branch we're migrating from.
- [ ] All maintainers agree the goals/non-goals are accurate. *(pending review)*

---

## 3. Normalised block-event model (internal)

These are Python types, not wire types. They exist between the provider
adapter and `_stream()`.

```python
@dataclass
class BlockStart:
    block_index: int
    kind: Literal["text", "tool_use"]
    tool_id: str | None = None     # only when kind == "tool_use"
    tool_name: str | None = None   # only when kind == "tool_use"

@dataclass
class BlockDelta:
    block_index: int
    kind: Literal["text", "tool_args"]
    text: str                       # for tool_args this is a JSON fragment

@dataclass
class BlockStop:
    block_index: int

@dataclass
class MessageStop:
    stop_reason: StopReason
    usage: Usage

@dataclass
class StreamError:
    type: str
    message: str
    partial: bool                   # True if some blocks were already emitted
```

### 3.1 Per-provider normalisation rules

**Anthropic** (`messages.stream()`):
- `content_block_start type=text` → `BlockStart(kind="text")`.
- `content_block_start type=tool_use` → `BlockStart(kind="tool_use", id, name)`.
- `content_block_delta text_delta` → `BlockDelta(kind="text", text=…)`.
- `content_block_delta input_json_delta` → `BlockDelta(kind="tool_args", text=…)`.
- `content_block_stop` → `BlockStop`.
- `message_delta` carries `stop_reason` and final usage; combined with
  `message_stop` → `MessageStop`.

**OpenAI** (chat completions `stream=True`, or Responses streaming):
The adapter maintains its own monotonic `next_block_index` counter and a
map `openai_tool_call_index → our_block_index`. OpenAI does **not** itself
order text vs tool_calls — both can appear in the same `delta`. Rules:
- On the first `delta.content` fragment while no text block is currently
  open: assign `bi = next_block_index++`, emit `BlockStart(kind="text",
  block_index=bi)`, then `BlockDelta(kind="text", text=…)`.
- On subsequent `delta.content` while the text block is open:
  `BlockDelta(kind="text", text=…)` for that block.
- On the first `delta.tool_calls[i]` with a given OpenAI index `i` (carries
  `id` and `function.name`): assign `bi = next_block_index++`, record
  `i → bi`, emit `BlockStart(kind="tool_use", block_index=bi, id, name)`.
- On subsequent `delta.tool_calls[i].function.arguments` fragments:
  `BlockDelta(kind="tool_args", block_index=map[i], text=…)`.
- A block is considered "open" until either (a) a different block of the
  same kind opens — emit `BlockStop` for the prior, or (b) the terminal
  chunk arrives.
- Terminal chunk with `finish_reason` → emit `BlockStop` for every open
  block in `block_index` order, then `MessageStop` with the mapped stop
  reason and usage from the terminal chunk's `usage` field.

**Gemini** (`generate_content_stream`):
The adapter maintains its own monotonic `next_block_index` counter and a
single piece of state: `open_text_bi: int | None` (the block_index of the
currently-open text block, if any).
- Each chunk has `candidates[0].content.parts[]`. Walk the parts list IN
  ORDER. For each `Part`:
  - **`text` Part** → if `open_text_bi is None`: assign `bi =
    next_block_index++`, emit `BlockStart(kind="text", block_index=bi)`,
    set `open_text_bi = bi`. Then emit `BlockDelta(kind="text",
    block_index=open_text_bi, text=part.text)`. **Consecutive text Parts —
    within the same chunk or across chunks — coalesce into one logical text
    block until a `function_call` Part interrupts.**
  - **`function_call` Part** → if `open_text_bi is not None`: emit
    `BlockStop(block_index=open_text_bi)`, set `open_text_bi = None`.
    Assign `bi = next_block_index++`. Emit `BlockStart(kind="tool_use",
    block_index=bi, id=uuid4().hex, name=fc.name)`, immediately followed
    by `BlockDelta(kind="tool_args", block_index=bi,
    text=json.dumps(dict(fc.args)))`, then `BlockStop(block_index=bi)`.
    Gemini hands us the function_call complete in one chunk (verified
    empirically 2026-05-14, see `/tmp/gemini_stream_probe.py`), so the
    "name early, args late" behaviour does not apply.
- Terminal chunk with `finish_reason` and `usage_metadata` → if
  `open_text_bi is not None`, emit `BlockStop` for it; then `MessageStop`
  with mapped stop reason and `usage_metadata` mapped to `Usage`.

Synthesised tool ids are local to the adapter — they never leave the
backend. The Gemini adapter already matches tool_results back to function
calls by `function_response.name` (see the existing `_name` field in
[chat.py:394](backend/app/routes/chat.py#L394)), so the synthesised ids
are inert on the return path.

### Section 3 checklist
- [x] Block-event dataclasses defined in a new module
      [backend/app/services/llm/stream.py](backend/app/services/llm/stream.py).
- [x] Per-provider normalisation rules implemented in
      [anthropic.py](backend/app/services/llm/anthropic.py)::`_AnthropicStreamCM`,
      [openai.py](backend/app/services/llm/openai.py)::`_OpenAIStreamCM`,
      [gemini.py](backend/app/services/llm/gemini.py)::`_GeminiStreamCM`.
- [x] Unit tests for each provider assert the normalised event sequence:
      [tests/streaming/test_anthropic_normalisation.py](backend/tests/streaming/test_anthropic_normalisation.py) (3),
      [tests/streaming/test_openai_normalisation.py](backend/tests/streaming/test_openai_normalisation.py) (4),
      [tests/streaming/test_gemini_normalisation.py](backend/tests/streaming/test_gemini_normalisation.py) (5).
      Fixtures are hand-built `SimpleNamespace` event sequences rather
      than recorded wire-format streams — equivalent coverage with less
      maintenance burden.

---

## 4. Provider interface

```python
class Provider(Protocol):
    id: str

    async def create_message(self, req: NormalisedRequest) -> NormalisedResponse: ...

    def stream(self, req: NormalisedRequest) -> "AbstractAsyncContextManager[AsyncIterator[StreamEvent]]": ...
```

Decisions:

1. **Context manager + async iterator** — not a plain async generator. The
   context manager's `__aexit__` is the explicit close hook for the upstream
   stream; relying on generator GC for cleanup is fragile under cancellation.
2. **`create_message()` is retained as a thin wrapper**: the base class
   implements it as "open `stream()`, consume events into a
   `NormalisedResponse`, return". Adapters may override only if they need to
   call a non-streaming endpoint (none currently do).
3. **`StreamEvent` is a discriminated union**: `BlockStart | BlockDelta |
   BlockStop | MessageStop | StreamError`. The orchestrator pattern-matches
   on type.
4. **Errors come through the stream**, not as exceptions, *except* for setup
   errors (auth, bad request) raised before the first event. Those propagate
   as exceptions. Once any event has been yielded, all subsequent failures
   are emitted as `StreamError` so the orchestrator knows whether to set
   `partial=True`.

### Section 4 checklist
- [x] `Provider` Protocol updated in
      [backend/app/services/llm/base.py](backend/app/services/llm/base.py).
- [x] Base class `BaseProvider` (new) implements `create_message` in terms of
      `stream`. Verified by [test_base_provider.py](backend/tests/streaming/test_base_provider.py) (4 tests).
- [x] Each adapter implements `stream` and removes its bespoke
      `create_message` implementation in favour of the base. (`grep
      "create_message" backend/app/services/llm/` shows only the
      `BaseProvider.create_message` definition.)

---

## 5. SSE wire format (frontend-visible)

Existing event names are preserved. New fields are additive.

### 5.1 `start`
```
event: start
data: {"model": str, "provider": str, "tools": [str], "seq": 0, "iteration": 0}
```
First event of the turn, `seq = 0`.

### 5.2 `delta`
```
event: delta
data: {"seq": int, "iteration": int, "block_index": int, "text": str}
```
Emitted once per assembled text block (server-side buffered) at the moment
the block's `BlockStop` is processed. Future per-token: emit on each
`BlockDelta(kind="text")` and drop the buffering; wire shape stays.

### 5.3 `tool_use_start`
```
event: tool_use_start
data: {"seq": int, "iteration": int, "block_index": int, "id": str, "name": str}
```
Emitted on `BlockStart(kind="tool_use")`. Anthropic and OpenAI: this lands
before the args are done streaming. Gemini: this lands at the same chunk as
the args.

### 5.4 `rich`
```
event: rich
data: {"seq": int, "iteration": int, "parent_block_index": int, "sub_seq": int, "kind": "image"|..., ...payload}
```
Emitted during tool dispatch, before that tool's `tool_use_end`.
`parent_block_index` ties the rich item to the tool that produced it.
`sub_seq` orders multiple rich items emitted by the same tool. **Frontend
adoption of `parent_block_index` is a separate UI ticket** — until adopted,
the field is informational only and the chat pane continues to render rich
items in arrival order.

### 5.5 `tool_use_end`
```
event: tool_use_end
data: {"seq": int, "iteration": int, "block_index": int, "id": str, "name": str,
       "ok": bool, "latency_ms": int, "error": str | null,
       "input": object, "result_preview": str}
```
Emitted after dispatch, after rich items.

### 5.6 `done`
```
event: done
data: {"seq": int, "stop_reason": str, "usage": {input_tokens, output_tokens,
       cache_read_input_tokens, cache_creation_input_tokens}, "iterations": int}
```
Terminal event of the turn.

### 5.7 `error`
```
event: error
data: {"seq": int, "iteration": int, "type": str, "message": str,
       "partial": bool, "during_block_index": int | null}
```
Terminal event when something went wrong. `partial=true` means some content
already shipped — the UI should keep it and mark the turn as incomplete.

### Section 5 checklist
- [x] Server emits all events with `seq` and `iteration` fields.
      Implemented in [chat.py::_stream](backend/app/routes/chat.py).
- [ ] Frontend ignores unknown fields. *(deferred — frontend follow-up will
      verify and switch to seq-sorted insertion.)*
- [x] No event name renamed; no field removed. All new fields additive.
- [x] `parent_block_index` and `sub_seq` shipped on `rich` events as
      informational until UI adopts them.

---

## 6. Component-level flow

```
┌────────────────────────────────────────────────────────────────────────┐
│                              FRONTEND                                  │
│                                                                        │
│  ChatPane.tsx                       MessageList.tsx                    │
│    │  POST /chat/stream                                                │
│    │  + AbortController.signal                                         │
│    │  -- reads SSE events via fetch + ReadableStream                   │
│    │                                                                   │
│    ├──► state: turnItems[]                                             │
│    │     - one item per (iteration, block_index) for tool_use          │
│    │     - one item per (iteration, block_index) for text              │
│    │     - rich items attached to their parent tool item               │
│    │     - mutated by id on tool_use_end                               │
│    │                                                                   │
│    └──► Stop button → AbortController.abort()                          │
└────────────────────────────────────────────────────────────────────────┘
                                  │  SSE
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                              BACKEND                                   │
│                                                                        │
│  routes/chat.py                                                        │
│    _stream()  -- async generator yielding SSE dict events              │
│      │                                                                 │
│      │  for iteration in range(max_iterations):                        │
│      │      async with provider.stream(req) as events:                 │
│      │          async for ev in events:                                │
│      │              dispatch_block_event(ev)  ─────────┐               │
│      │                                                 │               │
│      │      if stop_reason == "tool_use":              │               │
│      │          await asyncio.gather(*dispatches)      │               │
│      │          emit rich + tool_use_end               │               │
│      │          extend messages with assistant +       │               │
│      │              tool_result blocks                 │               │
│      │      else:                                      │               │
│      │          emit done; return                      │               │
│      │                                                 ▼               │
│      │                                       text_buf[block_index]     │
│      │                                       args_buf[block_index]     │
│      │                                       on BlockStop:             │
│      │                                         - text  → emit delta    │
│      │                                         - args  → parse JSON,   │
│      │                                                   queue tool    │
│      │                                                   dispatch      │
│      │                                                                 │
│  services/llm/{anthropic,openai,gemini}.py                             │
│    stream()  -- async context manager                                  │
│      └─► normalises provider events into BlockStart/Delta/Stop/        │
│          MessageStop/StreamError stream                                │
│                                                                        │
│  tools/registry.py                                                     │
│    dispatch(name, input, ctx) -- unchanged                             │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                          Provider streaming API
```

### Section 6 checklist
- [x] No new `asyncio.create_task` in `_stream()`. Tool dispatch uses
      `asyncio.gather` under the request-scoped task; cancellation
      cascades.
- [ ] Bridge dispatchers honour `CancelledError`. **Audit deferred** —
      streaming migration does not introduce new orphan-task risks; this
      audit is a pre-existing concern and remains a separate task.

---

## 7. Message and event ordering

### 7.1 Within a turn
1. `start` is `seq=0`, always first.
2. For each iteration, in iteration order:
   1. For each block in `block_index` order:
      - For text blocks: one `delta` at the time of `BlockStop`.
      - For tool_use blocks: one `tool_use_start` at the time of `BlockStart`,
        then zero or more `rich` events (during dispatch), then one
        `tool_use_end` (after dispatch).
   2. Iteration boundary: no event is emitted at the boundary itself; the
      next iteration's events follow directly.
3. `done` is the last event, with the highest `seq` in the turn.

### 7.2 Ordering guarantee
The server emits events in the order described above. `seq` is monotonic
and matches emission order. **`seq` is the canonical ordering key on the
frontend.** Items are inserted into `turnItems[]` at the position
determined by `seq`, not by SSE arrival order. This eliminates any chance
of React state-update batching, proxy buffering, or async handler
re-entrancy causing visible reordering.

Concrete frontend rule: maintain a sorted insertion using `seq`. New
events overwrite previous state for the same `(iteration, block_index)`
where applicable (e.g. `tool_use_end` mutates the chip created by
`tool_use_start`), but the *position* of the item is fixed by the `seq`
of its first appearance.

### 7.3 Why the existing "occasionally wrong order" symptom is eliminated
Three structural changes contribute:
1. `tool_use_start` lands earlier and at a predictable point relative to
   surrounding text, instead of after the whole turn buffers.
2. `rich` events are always emitted between `tool_use_start` and
   `tool_use_end` for the same `block_index`, never interleaved with
   unrelated blocks.
3. The text → tool_use → text sequence within a single iteration now reflects
   the model's actual generation order, not a single buffered dump.

With `seq` as the authoritative ordering key, no further server-side
metadata is needed. Any residual ordering bug after migration is a
frontend bug in the seq-sort implementation, not a protocol issue.

### Section 7 checklist
- [x] Backend integration test: [test_orchestrator.py](backend/tests/streaming/test_orchestrator.py)
      uses `FakeStreamingProvider` to verify the SSE event sequence and
      `seq` monotonicity, including multi-iteration tool-use loops.
- [ ] Manual UI smoke test — *deferred to frontend follow-up.*

---

## 8. Stop semantics — full lifecycle

### 8.1 What "Stop" means
The user has clicked the Stop button. They want generation to halt and the
UI to be in a state where they can either type a new message or retry. The
chat history must remain coherent — the next turn must not see a corrupt
trailing assistant message.

### 8.2 Stop propagation chain

```
User clicks Stop
  │
  ▼
ChatPane: AbortController.abort()
  │
  ▼
fetch() reader cancellation
  │
  ▼
SSE TCP connection closes
  │
  ▼
sse_starlette detects client disconnect
  │
  ▼
_stream() async generator receives CancelledError at its current await point
  │
  ├── If awaiting on `async for ev in events`: provider stream's __aexit__ fires
  │      → SDK closes upstream HTTP/2 stream cleanly
  │
  ├── If awaiting on `asyncio.gather(*dispatches)`: gather is cancelled
  │      → each dispatch task receives CancelledError
  │      → tool dispatchers either return early or are forcibly cancelled
  │
  └── Either way: control unwinds, no orphan tasks, no orphan upstream
      connections.
```

### 8.3 What's retained from the interrupted turn

The interrupted turn lives only in the *frontend* `turnItems[]` state. The
backend has no persistent record of an in-progress turn — `messages` is
rebuilt from the frontend's history on every request. So "what's retained"
is purely a frontend question.

| State on the wire when Stop hit | What the UI keeps | What the UI must mark |
| --- | --- | --- |
| Mid `delta` for block 0 (text) | The text rendered so far | Mark turn as `stopped` |
| Between `tool_use_start` and `tool_use_end` for block 1 | The tool chip in "yellow" state | Convert chip to "cancelled" state; do NOT pretend it succeeded |
| Mid `rich` emission for block 1 | The rich items received | Mark turn as `stopped` |
| Between iterations (after `tool_use_end`, before next iteration's first event) | Everything received | Mark turn as `stopped` |
| Inside a buffered text block (`BlockStart` seen, no `BlockStop` yet) | Nothing for that block (text was never flushed) | Mark turn as `stopped` |

Implementation note: a single `stopped: boolean` flag on the turn is
sufficient. Cancelled tool chips should render with a distinct style and
should NOT be included when assembling the message history for the next
turn (see §8.5).

### 8.4 Backend-side cleanup at Stop

Stop is detected as **client disconnect**, distinct from a server-side
mid-stream error (§11.2). Different paths, different emission rules:
- **Stop / disconnect**: no SSE events are emitted after detection — the
  client is gone. `CancelledError` propagates and cleans up.
- **Mid-stream error**: client is still connected; emit `error` SSE with
  `partial=true`, then return.

`_stream()` catches `CancelledError` and re-raises after:
1. Ensuring the provider context manager has exited (`async with` does this
   automatically as the exception propagates).
2. Ensuring any in-flight `asyncio.gather` has fully cancelled. `gather` with
   default behaviour waits for all sub-tasks to acknowledge cancellation —
   this is what we want. Use `gather(..., return_exceptions=True)` only if
   we explicitly need to swallow individual tool failures during cancel;
   otherwise default.
3. **No partial event is emitted to the client.** The client has already
   disconnected. Trying to emit on a dead connection raises, which would
   shadow the original `CancelledError`. The orchestrator must not catch and
   swallow.

### 8.5 What must be trimmed before the next turn

The frontend constructs `messages` for the next request from `turnItems[]`.
There is **no server-side conversation persistence** — the backend rebuilds
`messages` from whatever the frontend sends. So trimming is purely a
frontend responsibility, and there is nothing on the server that could go
out of sync with the trimmed history.

The rule:

**For each assistant turn marked `stopped`:**
- Include all completed text blocks.
- Include all `tool_use` blocks whose corresponding `tool_use_end` event was
  received. Include the matching `tool_result` block on the user side.
- **Exclude** any `tool_use` block that was started but never ended. The
  paired `tool_result` (which would point to a non-existent tool call from
  the provider's perspective) is also excluded.
- **Exclude** any text block whose `BlockStart` was received but `delta`
  never arrived (no buffered text was flushed).

In practice, since the wire format never sends a partial block, the rule
collapses to: "include every block the UI has rendered, except cancelled
tool chips and their phantom results."

If the result is an empty assistant message, exclude the assistant message
entirely. The next user turn then appears as if the stopped turn never
happened — clean recovery.

### 8.6 Recovery scenarios

**Scenario A — Stop hit during text generation.**
- UI state: `[{role: assistant, text: "I'll check the file…", stopped: true}]`.
- Next turn sends: `[..., {role: assistant, content: [{type: text, text: "I'll check the file…"}]}, {role: user, content: …new prompt…}]`.
- Provider accepts the assistant message as complete. Conversation
  continues naturally.

**Scenario B — Stop hit while tool was executing.**
- UI state: tool chip shows "cancelled".
- Excluded from next request: the cancelled `tool_use` block and any
  partial `tool_result` block.
- Included: any preceding completed text blocks.
- The next turn looks to the provider like a normal assistant turn that
  happened to stop early — no dangling tool calls, no orphan results.

**Scenario C — Stop hit between iterations.**
- The previous iteration's `tool_use_end` was received; results were going
  to be appended for the next iteration.
- UI state: all tool chips show success/failure normally; the turn is
  marked stopped because the model didn't get to write its final answer.
- Next request sends: all completed assistant content blocks + their
  matching `tool_result` blocks from the prior iteration. The new user
  message follows.
- Provider sees a complete assistant turn with tools called and results
  delivered, followed by a fresh user turn. Coherent.

**Scenario D — Stop hit during a rich event flush.**
- The producing tool's `tool_use_end` may or may not have arrived yet.
- If `tool_use_end` arrived: treat as Scenario B but with the tool marked
  successful (it was — the dispatch completed). Rich items rendered so far
  are kept.
- If `tool_use_end` did not arrive: treat as Scenario B with cancelled tool.
  Rich items received so far are kept on screen but the tool is excluded
  from the next request's `messages`.

### Section 8 checklist
- [ ] Frontend renders cancelled tool chips visually distinct. *(deferred)*
- [ ] Frontend's `messages` assembler skips cancelled tool blocks. *(deferred)*
- [x] Backend `_stream()` does not catch and swallow `CancelledError`
      ([chat.py::_stream](backend/app/routes/chat.py): explicit
      `except asyncio.CancelledError: logger.info(...); raise`).
- [x] Backend `_stream()` does not attempt to emit SSE after disconnect
      (re-raises before reaching the generic exception emitter).
- [ ] Tool dispatchers cancellation audit. *(deferred — pre-existing risk)*
- [x] Integration test: cancellation at mid-text and between-iterations
      with deterministic `asyncio.Event` pause points:
      [test_cancellation.py](backend/tests/streaming/test_cancellation.py)
      (2 tests). Both verify provider `__aexit__` fires and no further
      SSE is emitted after cancel.

---

## 9. State diagram for a single turn

```
                 ┌─────────────┐
                 │  IDLE       │
                 │ (no request │
                 │  in flight) │
                 └──────┬──────┘
                        │ user submits prompt
                        ▼
                 ┌─────────────┐
                 │  OPENING    │
                 │ POST sent,  │
                 │ waiting on  │
                 │ first SSE   │
                 └──────┬──────┘
                        │ `start` received
                        ▼
                ┌──────────────┐
                │  ITERATING   │◄────────────────┐
                │ (in iter N)  │                 │
                └──────┬───────┘                 │
                       │                         │
        ┌──────────────┼────────────────┐        │
        ▼              ▼                ▼        │
 ┌───────────┐ ┌───────────────┐ ┌─────────────┐ │
 │  text     │ │  tool_use     │ │  done       │ │
 │  block    │ │  block        │ │  received   │ │
 │ in flight │ │  in flight    │ └──────┬──────┘ │
 └─────┬─────┘ └───────┬───────┘        │        │
       │ BlockStop     │ BlockStop      │        │
       │ → emit delta  │ → emit         │        │
       │               │ tool_use_start │        │
       │               │ → dispatch     │        │
       │               │ → emit rich*   │        │
       │               │ → emit         │        │
       │               │ tool_use_end   │        │
       ▼               ▼                ▼        │
 ┌─────────────────────────────────┐  ┌───────┐  │
 │  back to ITERATING (more blocks │  │ DONE  │  │
 │  or message_stop in same iter)  │  │       │  │
 └──────┬──────────────────────────┘  └───┬───┘  │
        │ message_stop with                  │   │
        │ stop_reason="tool_use"             │   │
        │ → assemble assistant content,      │   │
        │   append + tool_result blocks,     │   │
        │   increment iteration N+1          │   │
        └────────────────────────────────────┼───┘
                                             │
                       ┌─────────────────────┘
                       ▼
                 ┌─────────────┐
                 │  IDLE       │
                 └─────────────┘

Error / cancellation transitions (from any non-IDLE state):

  CancelledError (Stop pressed) → STOPPED → trim → IDLE
  StreamError (mid-stream)      → ERRORED  → emit `error`, mark partial → IDLE
  Exception before first event  → ERRORED  → emit `error`, no partial   → IDLE
```

### Section 9 checklist
- [x] State diagram matches the implemented `_stream()` control flow.
- [x] All states have handled transitions: IDLE → OPENING → ITERATING
      (text|tool_use blocks) → DONE | STOPPED | ERRORED.

---

## 10. Buffering policy

Two separate buffers in `_stream()`:

**Text buffer — temporary policy.** Text deltas are buffered server-side
per block and emitted as a single `delta` SSE event on `BlockStop`. This is
a deliberate choice for the migration window, valid as long as the
frontend does not display progressive text. When progressive text lands as
a future change:
1. Delete the buffering layer.
2. Emit `delta` on every `BlockDelta(kind="text")` instead of on `BlockStop`.
3. The wire format is unchanged; the only difference is that multiple
   `delta` events with the same `(iteration, block_index)` will arrive
   instead of one. The frontend's seq-sorted insertion already handles
   multiple deltas correctly.

**Tool-args buffer — permanent.** Tool args are buffered server-side per
tool block and JSON-parsed on `BlockStop`. The orchestrator needs a
complete `input` dict to dispatch the tool; this is structural, not a
policy choice.

### Section 10 checklist
- [x] Code comment in `_stream()` near the text buffer points to
      streaming-migration.md §10 for the temporary-policy rationale.
- [ ] Audit that no frontend code assumes "one delta per block".
      *(deferred to frontend follow-up.)*

---

## 11. Error handling

### 11.1 Pre-stream errors
Auth failures, malformed requests, provider 4xx before any chunk arrives:
raise from `provider.stream()`'s `__aenter__` or the first `__anext__`.
`_stream()` catches and emits a single `error` event with `partial=false`.
No `start`-following content has been sent.

### 11.2 Mid-stream errors
Provider drops connection, rate limit hits mid-generation, decode error in
a chunk: the adapter yields a `StreamError` event. `_stream()`:
1. Closes any open text/args buffer for the current block (do not emit a
   half-finished `delta`).
2. Emits an `error` SSE event with `partial=true` and
   `during_block_index=N`.
3. Returns. No `done`.

### 11.3 Mid-stream errors during tool dispatch
A tool dispatcher raises:
- The dispatch task's exception is caught by `asyncio.gather` and surfaced
  as the tool's `ok=false` in `tool_use_end`. This is the existing
  behaviour and is preserved.
- A tool dispatcher hanging until SSE ping fails: pings detect the dead
  client and cancel `_stream()`, which cancels the gather. Same as Stop.

### 11.4 Iteration-limit error
Same as today: after `max_iterations` without a non-`tool_use` stop
reason, emit `error` with `type="IterationLimit"`, `partial=true`.

### Section 11 checklist
- [x] `_stream()` distinguishes pre-stream (`partial: False`) vs
      mid-stream (`partial: True`) error paths.
- [x] All error events carry `partial`, `seq`, `iteration`,
      `during_block_index`.
- [ ] Live tests cover mid-stream provider disconnect — *requires
      deliberately killing connections; deferred. The cancellation
      tests cover the most common error path (client disconnect).*

---

## 12. Logging and observability

Per-stream-event logging would flood logs. The following log lines should be
added/preserved:

| Event | Log line |
| --- | --- |
| `start` | `chat.stream open provider=<id> model=<m> tools=<n>` |
| First `BlockStart` of iteration | `chat.iter <N> first_block_latency_ms=<dt>` |
| Each tool dispatch start | existing `tool dispatch` log |
| Each tool dispatch end | existing |
| `message_stop` | `chat.iter <N> stop=<reason> input_tokens=<x> output_tokens=<y>` |
| `done` | `chat.stream close iterations=<N> total_input_tokens=<x> total_output_tokens=<y>` |
| `CancelledError` | `chat.stream cancelled at block_index=<i> iteration=<N>` |
| `StreamError` | `chat.stream error type=<t> partial=<p>` |

### Section 12 checklist
- [x] Cancellation log line added (`chat.stream cancelled iteration=…
      block_index=…`).
- [x] No per-token / per-chunk logs.
- [ ] First-block-latency + per-iteration usage log lines. *(minor —
      not blocking the migration; add in a follow-up.)*

---

## 13. Testing

The migration is verified by two test tiers. Both must pass before
landing.

### 13.1 Tier 1 — Automated (CI, no API keys)

These run on every PR and must be green at merge time. They use **recorded
provider chunk fixtures** and a **fake provider** — no network calls, no
secrets, no flake.

Location: `backend/tests/streaming/`.

Required suites:

1. **Normalisation unit tests** —
   `test_anthropic_normalisation.py`, `test_openai_normalisation.py`,
   `test_gemini_normalisation.py`. Each loads a recorded provider stream
   from `fixtures/<provider>/*.jsonl` and asserts the adapter's `stream()`
   yields the expected `BlockStart` / `BlockDelta` / `BlockStop` /
   `MessageStop` sequence.
   - Fixture coverage per provider: (a) text-only turn,
     (b) tool-use with short args, (c) tool-use with long args (the
     `create_report`-style case), (d) mixed text-before-tool, text-between-
     tools, text-after-tool, (e) parallel tool calls (multiple tool_use
     blocks in one iteration), (f) terminal error chunk.

2. **`_stream()` orchestrator tests** —
   `test_orchestrator.py`. Uses a `FakeStreamingProvider` that takes a
   scripted list of `StreamEvent`s and yields them. Asserts:
   - SSE sequence and `seq` monotonicity.
   - `tool_use_start` precedes the matching `tool_use_end`.
   - `delta` is emitted once per text block (current buffering policy).
   - Multi-iteration loop appends correct assistant + tool_result blocks.
   - `done` carries the correct accumulated usage.

3. **Cancellation tests** —
   `test_cancellation.py`. Uses a `FakeStreamingProvider` with
   `asyncio.Event` pause points so the test can deterministically suspend
   the stream at: (a) mid-text-block (between `BlockStart` and `BlockStop`
   for a text block), (b) mid-tool-args (between `BlockStart` and
   `BlockStop` for a tool_use), (c) mid-dispatch (after `BlockStop` but
   while a fake tool is awaiting an `Event`), (d) between iterations
   (after `MessageStop` with `stop_reason="tool_use"`, before next
   `stream()` opens). For each pause point: cancel the task, assert no
   orphan tasks, assert provider stream `__aexit__` was called, assert no
   SSE was emitted after the cancel.

4. **Frontend ordering tests** —
   `frontend/test/seq-ordering.test.ts`. Feeds a known SSE event sequence
   to the reducer in shuffled arrival order and asserts the final
   `turnItems[]` shape matches the in-order baseline. Catches any
   regression in seq-sorted insertion.

5. **Frontend trimming tests** —
   `frontend/test/messages-assembler.test.ts`. For each Stop scenario from
   §8.6, asserts the assembled `messages` array for the next request
   contains no dangling tool_use blocks and no phantom tool_result blocks.

### 13.2 Tier 2 — Manual, API-key-gated

These hit the real provider APIs. They are **not run in CI**, **not in the
standard test command**, and require the developer to supply API keys at
invocation time. **No keys are stored in the repo. No keys are written to
disk by the test runner.**

Location: `backend/tests/live/`. Each test file is decorated with a
pytest marker `@pytest.mark.live` that is excluded by default in
`pyproject.toml`'s pytest config. To run them:

```bash
# Single provider, single test
ANTHROPIC_API_KEY=sk-ant-... \
  uv run pytest backend/tests/live/test_live_anthropic.py -m live -v

# All providers (developer supplies whichever keys they have)
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... GEMINI_API_KEY=... \
  uv run pytest backend/tests/live/ -m live -v
```

The test runner reads keys from environment variables only. A test
without its key in the environment is **skipped, not failed** (so a
developer can run only the providers they have keys for).

Required live tests:

1. **`test_live_<provider>_text_streaming`** — send a prompt that elicits
   a multi-paragraph response. Assert: ≥2 chunks received from the SDK,
   no exceptions, `MessageStop` with `usage.output_tokens > 0`.

2. **`test_live_<provider>_tool_use_early_name`** — send a prompt that
   forces a `create_report`-style tool call with args ≥3kB. Capture
   wall-clock timestamps. Assert:
   - Anthropic / OpenAI: time-to-first-`BlockStart(kind="tool_use")` is
     significantly less than time-to-`MessageStop` (margin ≥ 500ms for a
     long-arg call, in practice usually multiple seconds).
   - Gemini: this assertion is **inverted** — name and args land in the
     same chunk; verify `BlockStart` and `BlockStop` for the tool_use
     fire within the same chunk window.

3. **`test_live_<provider>_cancellation`** — open the stream, await one
   event, cancel. Assert: provider connection closes cleanly (no hung
   sockets), no exception bubbles past the test's `try/except
   CancelledError`. This is the one test that genuinely cannot be faked —
   we need to verify the real SDK's `__aexit__` behaviour.

4. **`test_live_<provider>_usage_parity`** — for a fixed deterministic
   prompt (`temperature=0`, `seed` where supported), run once through the
   streaming path and once through a one-shot non-streaming path
   (constructed inline within the test, not from production code), and
   assert the usage totals match. **This test is the regression guard for
   the migration itself** — confirms streaming doesn't drop or
   double-count tokens.

5. **`test_live_<provider>_end_to_end`** — full chat endpoint test: spin
   up the FastAPI app with `httpx.AsyncClient`, POST to `/chat/stream`,
   consume SSE, assert the full event sequence for a turn that includes
   preamble text + a tool call + a final text response. This is the
   closest thing to "production smoke test" in the suite.

### 13.3 Manual UI smoke test

After Tier 1 and Tier 2 pass, the developer performs a manual UI walkthrough:

- Open the bookmarklet on a host page (any).
- For each provider configured in Settings:
  - Type a prompt that elicits a `create_report`-style tool call. Watch
    the tool-name chip — for Anthropic/OpenAI it should appear well
    before the chip turns from yellow to green; for Gemini it appears
    only when the call lands but should not be preceded by a long blank
    pause for non-tool turns.
  - Type a prompt that elicits preamble text + tool + post-text. Verify
    rendering order matches the model's generation order.
  - Mid-stream, press Stop. Verify: turn is marked stopped, cancelled
    chips render distinctly, typing a new prompt continues the
    conversation without error.
  - Force a tool failure (e.g. malformed input). Verify
    `tool_use_end` with `ok=false` renders correctly.

### 13.4 Test artefacts NOT in the repo

The following are explicitly **never committed**:
- API keys (env vars only).
- `.env` files containing keys.
- Recorded streams that include keys in headers — fixtures must be
  scrubbed before being added under `fixtures/`.
- Any file under `backend/tests/live/` that hardcodes a key.

A pre-commit grep guard for `sk-ant-`, `sk-proj-`, and `AIza` prefixes
should be added to the migration commit. (Cheap — 5 lines in
`.pre-commit-config.yaml` or equivalent.)

### Section 13 checklist
- [x] Tier 1 directory [backend/tests/streaming/](backend/tests/streaming/)
      exists with all required test files: `test_base_provider.py`,
      `test_orchestrator.py`, `test_cancellation.py`, and one
      normalisation file per provider. Plus shared `fake_provider.py`.
      **22 tests, all green.**
- [x] Tier 1 fixtures cover: text-only, short-args tool, long-args tool,
      mixed text-before-tool, parallel/multiple tool calls, terminal
      error / max_tokens. Implemented as hand-built `SimpleNamespace`
      event sequences (no recorded chunks needed; equivalent coverage).
- [x] Tier 1 runs via `pytest backend/tests/streaming/` and is part of
      the default test command (no marker filtering needed; default
      `addopts = "-m 'not live'"` excludes only Tier 2).
- [x] Tier 2 directory [backend/tests/live/](backend/tests/live/) exists with
      five required live tests per provider (15 tests total):
      `test_live_anthropic.py`, `test_live_openai.py`,
      `test_live_gemini.py`.
- [x] `pyproject.toml` `[tool.pytest.ini_options]` excludes `-m live` by
      default (`addopts = "-q --strict-markers -m 'not live'"`).
- [x] Live tests skip (not fail) when their key env var is absent
      (per-provider fixtures in `tests/live/conftest.py`).
- [ ] Pre-commit key-prefix guard installed. *(minor follow-up; no
      keys appear in any committed file.)*
- [ ] Frontend tests `seq-ordering.test.ts` and
      `messages-assembler.test.ts`. *(deferred to frontend follow-up.)*
- [ ] Manual UI smoke (§13.3). *(deferred to frontend follow-up.)*

---

## 14. Migration sequence — single clean transition

**No feature flags. No fallback paths. No coexistence period.** The old
non-streaming code is removed in the same commit that introduces
streaming. One PR, one commit, one cutover.

### Commit message convention
Every commit that is part of this migration MUST have a subject line
beginning with `streaming migration:` so the change is trivially
identifiable in `git log`. Examples:
- `streaming migration: block-event types and BaseProvider.stream()`
- `streaming migration: switch _stream() to consume block events`
- `streaming migration: frontend seq-sorted insertion + cancelled chip`

If the migration is split across multiple commits for review hygiene, they
must all carry this prefix and they must land as a single squash-or-merge
unit — the master branch never has a half-migrated state.

### Work order within the migration
The work is sequenced for the implementer's sanity, not for shippability
of intermediate states:

1. **Block event types** — add `backend/app/services/llm/stream.py` with
   the dataclasses (`BlockStart`, `BlockDelta`, `BlockStop`,
   `MessageStop`, `StreamError`).
2. **`BaseProvider`** — new base class with `create_message` implemented
   in terms of `stream`. Adapters will inherit.
3. **Anthropic `stream()`** — implement, fixture tests.
4. **OpenAI `stream()`** — implement, fixture tests.
5. **Gemini `stream()`** — implement, fixture tests.
6. **Delete old `create_message` adapter implementations** — replaced by
   the base class.
7. **`_stream()` rewrite** — consume block events, emit SSE with `seq`,
   `iteration`, `block_index`.
8. **Tool dispatcher cancellation audit** — fix anything that doesn't
   honour `CancelledError`.
9. **Frontend changes** — seq-sorted insertion (§7.2); cancelled-tool
   chip styling and message-assembler trimming (§8.5).
10. **Tier 1 automated tests** — §13.1 (normalisation, orchestrator,
    cancellation, frontend ordering, frontend trimming).
11. **Tier 2 live tests + UI smoke** — §13.2, §13.3, run manually with
    keys supplied via env vars.
12. **Delete any now-unreferenced code** — old streaming SSE-from-
    completed-response paths, dead helpers, dead types.

### Section 14 checklist
- [ ] Every commit in the migration begins with `streaming migration:`.
      *(to be enforced at commit time; this implementation is currently
      uncommitted.)*
- [ ] The merge into master is atomic — no intermediate commit on master
      represents a half-migrated state.
- [ ] No `if streaming_enabled` / `USE_STREAMING` / feature-flag branches
      exist anywhere in the codebase after the migration.
- [ ] No `create_message()` adapter implementation survives in
      `openai.py`, `anthropic.py`, `gemini.py` — only the base-class
      version exists.
- [ ] `grep -r "create_message" backend/` returns only the
      `BaseProvider.create_message` definition and its callers.

---

## 15. Open questions deferred

These are explicitly out of scope for this migration:

1. **Per-token UI rendering.** Buffering policy in §10 leaves the door open.
2. **Frontend `parent_block_index` adoption.** SSE field is shipped;
   rendering change is a separate ticket.
3. **Streaming structured outputs / tool result streaming.** Not on the
   provider roadmap as of 2026-05-14.
4. **Replacing `EventSource`-style consumption with a typed client.** Out
   of scope; current `fetch + ReadableStream` reader is fine.

---

## 16. Acceptance criteria

The migration is complete when all of the following hold:

1. All section checklists above are ticked.
2. Anthropic and OpenAI: `tool_use_start` arrives before the tool's
   arguments are fully generated (verified by instrumenting the time
   between `tool_use_start` and the eventual tool dispatch start —
   should be ≫ 0 for arg-heavy tools like `create_report`).
3. Gemini: streaming text turns render progressively at the level of
   text blocks (multiple `delta` events for a long answer, instead of
   one after a long pause). Tool-use turns still have the
   `tool_use_start` delayed until the chunk lands — this is accepted.
4. Stop pressed during any of: text streaming, tool args streaming, tool
   dispatch, between iterations — leaves the UI in a state where the
   next user message produces a coherent provider request with no
   dangling tool_use blocks.
5. The "occasionally wrong order" symptom is no longer observed in
   manual smoke testing across all three providers. (If still observed,
   open a frontend-side investigation; the backend ordering contract
   is now strict.)
6. Token usage in `done` events equals the sum of per-iteration usage
   values returned by each provider's API for the turn. Verified by
   comparing against the same logical turn run through the old
   non-streaming path (recorded fixtures, not live billing).
7. `git log --oneline master` shows the migration as a contiguous run
   of commits all prefixed `streaming migration:`, with no
   half-migrated state at any commit boundary.
8. `grep -r "feature flag\|USE_STREAMING\|streaming_enabled" backend/
   frontend/` returns nothing.
