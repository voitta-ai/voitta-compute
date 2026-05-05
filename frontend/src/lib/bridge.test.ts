// Bridge primitive registration + PrimitiveError shape.
//
// `startBridge()` itself depends on EventSource + fetch + DOM —
// integration-tested with the running backend. Here we cover the parts
// that are pure: primitive registration accumulator, error class shape.

import { describe, expect, it } from "vitest";
import { PrimitiveError, registerPrimitive } from "./bridge";

describe("PrimitiveError", () => {
  it("preserves kind and message; details optional", () => {
    const e = new PrimitiveError("not_found", "buffer XYZ", { id: "XYZ" });
    expect(e).toBeInstanceOf(Error);
    expect(e.kind).toBe("not_found");
    expect(e.message).toBe("buffer XYZ");
    expect(e.details).toEqual({ id: "XYZ" });
  });

  it("works without details", () => {
    const e = new PrimitiveError("invalid_args", "missing handle");
    expect(e.kind).toBe("invalid_args");
    expect(e.details).toBeUndefined();
  });
});

describe("registerPrimitive", () => {
  it("accepts a name + async handler without throwing", () => {
    expect(() =>
      registerPrimitive("test_noop", async () => ({ ok: true })),
    ).not.toThrow();
  });
});
