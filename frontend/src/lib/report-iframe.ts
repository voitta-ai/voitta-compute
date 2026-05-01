// Registry for the currently-mounted report iframe. ReportPane sets this
// on mount and clears it on unmount; the `screenshot_report` browser
// primitive reads it at call time.
//
// A single global is fine — only one ReportPane is rendered at a time
// (ChatPane sets at most one `activeReport`). If that ever changes, we
// can swap this out for a Map keyed by report_id.

let activeReportIframe: HTMLIFrameElement | null = null;
let activeReportInfo: { report_id: string; url: string; title?: string } | null = null;

export function setActiveReportIframe(
  iframe: HTMLIFrameElement | null,
  info: { report_id: string; url: string; title?: string } | null,
): void {
  activeReportIframe = iframe;
  activeReportInfo = info;
}

export function getActiveReportIframe(): HTMLIFrameElement | null {
  return activeReportIframe;
}

export function getActiveReportInfo():
  | { report_id: string; url: string; title?: string }
  | null {
  return activeReportInfo;
}
