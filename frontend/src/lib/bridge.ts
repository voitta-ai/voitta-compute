// Compat shim for plugins ported from the SSE-bridge fork.
//
// The legacy bookmarklet used a custom SSE bridge module at
// ``frontend/src/lib/bridge.ts`` that exported ``registerPrimitive``,
// ``PrimitiveError``, ``getBackendOrigin``, and others. Chainlit's
// react-client owns the transport now, so we don't need the bridge
// itself — but ported plugin widgets still ``import { ... } from
// "../../../frontend/src/lib/bridge"``. This shim re-exports the
// pieces those imports need so the build resolves without touching
// every plugin's ``widget.ts``.
//
// When porting a plugin, prefer importing from
// ``../../../frontend/src/lib/primitives`` directly; this file is
// here for backwards compatibility.

export { registerPrimitive } from "./primitives";
export type { Primitive } from "./primitives";

export class PrimitiveError extends Error {
  kind: string;
  details?: Record<string, unknown>;
  constructor(kind: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.kind = kind;
    this.details = details;
  }
}

export function getBackendOrigin(): string {
  // The Chainlit ChainlitAPI is constructed once with the bookmarklet's
  // origin and stored in the React context; plugins that need to fetch
  // BE-side assets can read it from window.location since the FE is
  // served by the same BE.
  return window.location.origin;
}

export function getSessionId(): string {
  // Plugins that needed a stable session ID used this to namespace
  // requests over the old bridge. Chainlit owns sessions now — return
  // empty string and let callers fall back to no-op behaviour.
  return "";
}
