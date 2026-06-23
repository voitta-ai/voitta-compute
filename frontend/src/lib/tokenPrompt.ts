// Bridge between the backend's ``prompt_claude_token`` call_fn round-trip and
// the masked-input modal. The backend awaits the call_fn ACK; CallFnRouter
// calls ``requestToken`` and resolves that ACK with whatever the modal returns.
//
// The token rides the call_fn ACK only — it is never sent as a chat message,
// so it never lands in a Chainlit step or the conversation DB.

export interface TokenPromptResult {
  token?: string;
  cancelled?: boolean;
}

interface Pending {
  instructions: string;
  resolve: (r: TokenPromptResult) => void;
}

let pending: Pending | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** Open the modal and resolve when the user submits or cancels. */
export function requestToken(instructions: string): Promise<TokenPromptResult> {
  // If a prompt is somehow already open, cancel it first.
  if (pending) {
    const prev = pending;
    pending = null;
    prev.resolve({ cancelled: true });
  }
  return new Promise<TokenPromptResult>((resolve) => {
    pending = { instructions, resolve };
    emit();
  });
}

export function getPending(): Pending | null {
  return pending;
}

export function resolvePending(result: TokenPromptResult): void {
  if (!pending) return;
  const p = pending;
  pending = null;
  emit();
  p.resolve(result);
}

export function subscribePending(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
