import { describe, expect, it, vi } from "vitest";
import { createTelemetryClient } from "./telemetry";

describe("recommendation telemetry client", () => {
  it("batches events with stable idempotency keys", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    let eventNumber = 0;
    const client = createTelemetryClient({
      fetchImpl: fetchMock,
      randomUUID: () => `fixed-event-id-${++eventNumber}`,
      flushDelayMs: 60_000,
    });
    client.enqueue({ candidate_id: 7, event_type: "visible", run_id: "run-1234" });
    client.enqueue({ candidate_id: 8, event_type: "detail_open", run_id: "run-1234" });
    await client.flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.method).toBe("POST");
    expect(JSON.parse(String(request.body))).toEqual({
      events: [
        {
          candidate_id: 7,
          event_type: "visible",
          run_id: "run-1234",
          event_key: "ui-fixed-event-id-1",
          source: "ui",
        },
        {
          candidate_id: 8,
          event_type: "detail_open",
          run_id: "run-1234",
          event_key: "ui-fixed-event-id-2",
          source: "ui",
        },
      ],
    });
    client.destroy();
  });

  it("uses sendBeacon for page-exit flushes and tolerates transport failure", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    const beaconMock = vi.fn().mockReturnValue(true);
    const client = createTelemetryClient({
      fetchImpl: fetchMock,
      beaconImpl: beaconMock,
      randomUUID: () => "beacon-event-id",
      flushDelayMs: 60_000,
    });
    client.enqueue({ candidate_id: 7, event_type: "source_open" });
    await expect(client.flush({ beacon: true })).resolves.toBeUndefined();
    expect(beaconMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
    client.enqueue({ candidate_id: 7, event_type: "detail_open" });
    await expect(client.flush()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    client.destroy();
  });

  it("drops malformed client-side events without throwing", async () => {
    const fetchMock = vi.fn();
    const client = createTelemetryClient({ fetchImpl: fetchMock, flushDelayMs: 60_000 });
    client.enqueue({ candidate_id: 0, event_type: "visible" });
    await client.flush();
    expect(fetchMock).not.toHaveBeenCalled();
    client.destroy();
  });
});
