import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { postRenderEvent } from "../postRenderEvent";

describe("postRenderEvent", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("{}"));
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts to /api/report-render-events with credentials", async () => {
    await postRenderEvent("https://example.com", {
      name: "demo",
      kind: "ready",
      render_id: "abc",
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("https://example.com/api/report-render-events");
    expect(opts.method).toBe("POST");
    expect(opts.credentials).toBe("include");
    const body = JSON.parse(opts.body);
    expect(body).toMatchObject({ name: "demo", kind: "ready", render_id: "abc" });
  });

  it("swallows fetch errors", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("network"));
    await expect(
      postRenderEvent("x", { name: "n", kind: "ready" }),
    ).resolves.toBeUndefined();
  });
});
