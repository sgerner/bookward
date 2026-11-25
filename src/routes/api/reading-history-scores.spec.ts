import { afterEach, describe, expect, it, vi } from "vitest";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

vi.mock("#lib/server/engine.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("#lib/server/engine.js")>()),
  engine: engineMock,
}));

import { EngineError } from "#lib/server/engine.js";
import { GET } from "./reading-history/scores/+server";

afterEach(() => vi.clearAllMocks());

describe("reading-history scores endpoint", () => {
  it("defaults to a bounded 200-row batch and forwards valid 200-row requests", async () => {
    engineMock.mockResolvedValue([
      {
        id: 11,
        algorithm_score: 88.4,
        algorithm_explanation: ["Strong thematic match"],
        metadata_confidence: 1,
      },
    ]);

    const defaultResponse = await GET({
      url: new URL("http://bookward.test/api/reading-history/scores"),
    } as never);
    expect(defaultResponse.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/reading-history/scores?offset=0&limit=200",
    );
    expect(await defaultResponse.json()).toEqual({
      scores: [
        {
          id: 11,
          algorithm_score: 88.4,
          algorithm_explanation: ["Strong thematic match"],
          metadata_confidence: 1,
        },
      ],
    });

    const pagedResponse = await GET({
      url: new URL(
        "http://bookward.test/api/reading-history/scores?offset=200&limit=200",
      ),
    } as never);
    expect(pagedResponse.status).toBe(200);
    expect(engineMock).toHaveBeenLastCalledWith(
      "/api/reading-history/scores?offset=200&limit=200",
    );
  });

  it("rejects out-of-range score batch parameters before contacting the engine", async () => {
    for (const invalidQuery of [
      "limit=0",
      "limit=201",
      "offset=-1",
      "limit=20.5",
    ]) {
      const response = await GET({
        url: new URL(
          `http://bookward.test/api/reading-history/scores?${invalidQuery}`,
        ),
      } as never);
      expect(response.status, invalidQuery).toBe(400);
    }
    expect(engineMock).not.toHaveBeenCalled();
  });

  it("forwards upstream engine errors cleanly so failed batches can be retried", async () => {
    engineMock.mockRejectedValueOnce(
      new EngineError("Upstream score batch failed.", 502),
    );

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/reading-history/scores?offset=200&limit=200",
      ),
    } as never);
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      message: "Upstream score batch failed.",
    });
  });
});
