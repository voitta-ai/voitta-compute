// Buffer store — put/get/delete/list/clear, partial-key delete.
//
// Tests the in-memory buffer surface that primitives-buffers.ts and
// buffer_eval consume. Pure logic; no DOM required.

import { afterEach, describe, expect, it } from "vitest";
import {
  bufferClear,
  bufferDelete,
  bufferDeleteKeys,
  bufferGet,
  bufferList,
  bufferPut,
  bufferTotals,
  curveSeries,
  filterCurves,
  getMetaValue,
} from "./buffers";

afterEach(() => {
  bufferClear();
});

describe("bufferPut / bufferGet", () => {
  it("returns a handle and a record with the same shape", () => {
    const rec = bufferPut({ foo: 1 }, "generic", { items: 1 }, { src: "test" });
    expect(rec.handle).toMatch(/^buf_/);
    expect(rec.kind).toBe("generic");
    expect(rec.summary).toEqual({ items: 1 });
    expect(rec.meta).toEqual({ src: "test" });
    expect(rec.bytes).toBeGreaterThan(0);

    const got = bufferGet(rec.handle);
    expect(got).not.toBeNull();
    expect(got!.data).toEqual({ foo: 1 });
  });

  it("issues unique handles", () => {
    const a = bufferPut({}, "generic", null);
    const b = bufferPut({}, "generic", null);
    expect(a.handle).not.toEqual(b.handle);
  });

  it("returns null for unknown handle", () => {
    expect(bufferGet("buf_nope")).toBeNull();
  });
});

describe("bufferList / bufferTotals", () => {
  it("totals reflect put/delete", () => {
    expect(bufferTotals().count).toBe(0);
    const r = bufferPut({ x: "hello" }, "generic", null);
    const t = bufferTotals();
    expect(t.count).toBe(1);
    expect(t.bytes).toBeGreaterThan(0);

    bufferDelete(r.handle);
    expect(bufferTotals().count).toBe(0);
  });

  it("list omits raw data, keeps shape", () => {
    bufferPut({ a: 1 }, "kind-a", { hint: "a" });
    bufferPut({ b: 2 }, "kind-b", { hint: "b" });
    const list = bufferList();
    expect(list).toHaveLength(2);
    expect(list[0]).toHaveProperty("handle");
    expect(list[0]).toHaveProperty("kind");
    expect(list[0]).toHaveProperty("summary");
    expect(list[0]).not.toHaveProperty("data");
  });
});

describe("bufferClear", () => {
  it("frees everything and reports counts", () => {
    bufferPut({}, "x", null);
    bufferPut({}, "y", null);
    const out = bufferClear();
    expect(out.freed_count).toBe(2);
    expect(bufferList()).toHaveLength(0);
  });
});

describe("bufferDeleteKeys", () => {
  it("removes top-level keys and reports them", () => {
    const rec = bufferPut(
      { keep: 1, drop: { inner: "x" }, list: [1, 2, 3] },
      "generic",
      null,
    );
    const out = bufferDeleteKeys(rec.handle, ["drop", "list"]);
    expect(out.ok).toBe(true);
    expect(out.dropped.sort()).toEqual(["drop", "list"]);
    expect(out.not_found).toEqual([]);
    expect(out.bytes_after).toBeLessThan(out.bytes_before);
    const got = bufferGet(rec.handle);
    expect(got!.data).toEqual({ keep: 1 });
  });

  it("reports unknown keys as not_found", () => {
    const rec = bufferPut({ a: 1 }, "generic", null);
    const out = bufferDeleteKeys(rec.handle, ["nope"]);
    expect(out.ok).toBe(true);
    expect(out.dropped).toEqual([]);
    expect(out.not_found).toEqual(["nope"]);
  });
});

describe("curve helpers (data-shape utilities)", () => {
  const curves = [
    {
      name: "voltage",
      metadata: [
        { key: "channel", value: "A" },
        { key: "unit", value: "V" },
      ],
      series: [
        { name: "x", values: [0, 1, 2] },
        { name: "y", values: [10, 20, 30] },
      ],
    },
    {
      name: "current",
      metadata: [{ key: "channel", value: "B" }],
      series: [
        { name: "x", values: [0, 1, 2] },
        { name: "y", values: [1, 2, 3] },
      ],
    },
  ];

  it("getMetaValue returns the named metadata entry's value", () => {
    expect(getMetaValue(curves[0], "channel")).toBe("A");
    expect(getMetaValue(curves[0], "missing")).toBeNull();
  });

  it("filterCurves matches every metadata key", () => {
    const out = filterCurves(curves, { channel: "B" });
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("current");
  });

  it("filterCurves with an empty filter returns all", () => {
    expect(filterCurves(curves, {})).toHaveLength(2);
  });

  it("curveSeries returns the named series array", () => {
    expect(curveSeries(curves[0], "y")).toEqual([10, 20, 30]);
    expect(curveSeries(curves[0], "missing")).toBeNull();
  });
});
