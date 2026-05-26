import { atom } from "recoil";
import type { ActiveReport } from "./types";

export const reportsState = atom<ActiveReport[]>({
  key: "voitta.reports",
  default: [],
});

// "workspace" | render_id of the focused report | null (nothing open yet)
export const activeTabState = atom<string | null>({
  key: "voitta.activeTab",
  default: null,
});

// Whether the workspace tab is present in the tab bar.
export const workspaceTabOpenState = atom<boolean>({
  key: "voitta.workspaceTabOpen",
  default: false,
});

// Collapsed: entire pane minimised to an edge handle (main × button).
export const reportCollapsedState = atom<boolean>({
  key: "voitta.reportCollapsed",
  default: false,
});

// True while a report is being fetched/rendered — shows a spinner overlay.
export const reportLoadingState = atom<boolean>({
  key: "voitta.reportLoading",
  default: false,
});
