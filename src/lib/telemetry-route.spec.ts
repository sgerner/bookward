import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "../routes/api/telemetry/+server";

afterEach(() => vi.unstubAllGlobals());

describe("same-origin telemetry endpoint", () => {
  it("forwards bounded event batches to the engine", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST({
      request: new Request("http://bookward.test/api/telemetry", {
        method: "POST",
        body: JSON.stringify({ events: [{ event_key: "ui-event-1", candidate_id: 7, event_type: "visible" }] }),
        headers: { "content-type": "application/json" },
      }),
    } as never);
    expect(response.status).toBe(202);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/telemetry/events",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ events: [{ event_key: "ui-event-1", candidate_id: 7, event_type: "visible" }] }),
      }),
    );
  });

  it("silently ignores malformed or unavailable telemetry", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    const malformed = await POST({
      request: new Request("http://bookward.test/api/telemetry", {
        method: "POST",
        body: JSON.stringify({ events: [] }),
        headers: { "content-type": "application/json" },
      }),
    } as never);
    expect(malformed.status).toBe(204);
    expect(fetchMock).not.toHaveBeenCalled();

    const unavailable = await POST({
      request: new Request("http://bookward.test/api/telemetry", {
        method: "POST",
        body: JSON.stringify({ events: [{ event_key: "ui-event-2", candidate_id: 7, event_type: "visible" }] }),
        headers: { "content-type": "application/json" },
      }),
    } as never);
    expect(unavailable.status).toBe(202);
  });
});
