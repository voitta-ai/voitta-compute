import { describe, expect, it } from "vitest";
import { snapshot_UNSTABLE } from "recoil";
import { activeReportState, reportCollapsedState } from "../state";
import type { ActiveReport } from "../types";

describe("report state atoms", () => {
  it("defaults to no active report", () => {
    const snap = snapshot_UNSTABLE();
    expect(snap.getLoadable(activeReportState).valueOrThrow()).toBeNull();
    expect(snap.getLoadable(reportCollapsedState).valueOrThrow()).toBe(false);
  });

  it("accepts an ActiveReport", () => {
    const r: ActiveReport = {
      name: "demo",
      title: "Demo",
      render_id: "abc",
      payload: { kind: "pyplot", data: "x", mime: "image/png", width: 1, height: 1 },
    };
    const snap = snapshot_UNSTABLE(({ set }) => set(activeReportState, r));
    expect(snap.getLoadable(activeReportState).valueOrThrow()).toEqual(r);
  });
});
