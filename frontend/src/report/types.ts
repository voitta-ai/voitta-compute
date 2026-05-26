// Wire types for report payloads — mirrors backend/app/reports/schemas.py.
//
// One kind: ``html``. Scripts return raw HTML strings; the BE renders
// + caches them + injects the screenshot shim into <head>. The FE
// mounts <iframe src="/api/html-report?id=...&render_id=...">.

export interface HtmlPayload {
  kind: "html";
  url: string;
  title?: string | null;
}

export type RenderPayload = HtmlPayload;

// Argument shape the BE ships in the show_html_report call_fn.
export interface ShowHtmlReportArgs {
  name: string;
  title?: string | null;
  render_id: string;
  url: string;
  kind: "html";
}

// The Recoil atom's value when a report is mounted.
export interface ActiveReport {
  name: string;
  title: string | null;
  render_id: string;
  payload: RenderPayload;
}
