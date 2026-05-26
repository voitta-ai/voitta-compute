// POST to /api/report-render-events. Fire-and-forget; we log failures
// but never throw, because a render-event drain hiccup must not
// affect the rendered pane.

export type RenderEventKind = "ready" | "error" | "inventory" | "info";

export interface RenderEventBody {
  name: string;
  kind: RenderEventKind;
  render_id?: string;
  message?: string;
  detail?: Record<string, unknown>;
  inventory?: Record<string, unknown> | null;
}

export async function postRenderEvent(
  backendOrigin: string,
  body: RenderEventBody,
): Promise<void> {
  try {
    await fetch(`${backendOrigin}/api/report-render-events`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      credentials: "include",
    });
  } catch (err) {
    console.warn("[voitta] render-event POST failed", err);
  }
}
