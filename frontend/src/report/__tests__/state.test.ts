import { describe, expect, it } from "vitest";
import { snapshot_UNSTABLE } from "recoil";
import { reportsState, reportCollapsedState } from "../state";
import type { ActiveReport } from "../types";

describe("report state atoms", () => {
  it("defaults to empty reports list", () => {
    const snap = snapshot_UNSTABLE();
    expect(snap.getLoadable(reportsState).valueOrThrow()).toEqual([]);
    expect(snap.getLoadable(reportCollapsedState).valueOrThrow()).toBe(false);
  });

  it("accepts an ActiveReport", () => {
    const r: ActiveReport = {
      name: "demo",
      title: "Demo",
      render_id: "abc",
      payload: { kind: "html", url: "/api/html-report?id=demo&render_id=abc" },
    };
    const snap = snapshot_UNSTABLE(({ set }) => set(reportsState, [r]));
    expect(snap.getLoadable(reportsState).valueOrThrow()).toEqual([r]);
  });
});
