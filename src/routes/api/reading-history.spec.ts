import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineError } from "#lib/server/engine.js";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

vi.mock("#lib/server/engine.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("#lib/server/engine.js")>()),
  engine: engineMock,
}));

import { GET, PUT } from "./reading-history/+server";
import { PUT as PUT_BY_ID } from "./reading-history/[id]/+server";

afterEach(() => vi.clearAllMocks());

describe("reading-history endpoints", () => {
  it("loads reading history items from the engine", async () => {
    engineMock.mockResolvedValue([
      {
        id: 3,
        title: "The Left Hand of Darkness",
        author: "Ursula K. Le Guin",
        rating: null,
        read_at: "2026-10-01",
        source: "manual",
        created_at: "2026-10-01T12:00:00Z",
        rank_score: 88.2,
        algorithm_score: null,
        algorithm_explanation: [],
        metadata_confidence: null,
      },
    ]);

    const response = await GET({} as never);
    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith("/api/reading-history");
    expect(await response.json()).toEqual({
      items: [
        expect.objectContaining({
          id: 3,
          title: "The Left Hand of Darkness",
          rating: null,
        }),
      ],
    });
  });

  it("updates and clears ratings via PUT on /api/reading-history", async () => {
    engineMock.mockResolvedValueOnce({
      id: 3,
      title: "The Left Hand of Darkness",
      author: "Ursula K. Le Guin",
      rating: 5,
      read_at: "2026-10-01",
      source: "manual",
      created_at: "2026-10-01T12:00:00Z",
      job_id: "job-score-1",
    });

    const putResponse = await PUT({
      request: new Request("http://bookward.test/api/reading-history", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: 3, rating: 5 }),
      }),
    } as never);

    expect(putResponse.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith("/api/reading-history/3", {
      method: "PUT",
      body: JSON.stringify({ rating: 5 }),
    });
    expect(await putResponse.json()).toEqual({
      item: {
        id: 3,
        title: "The Left Hand of Darkness",
        author: "Ursula K. Le Guin",
        rating: 5,
        read_at: "2026-10-01",
        source: "manual",
        created_at: "2026-10-01T12:00:00Z",
      },
      job_id: "job-score-1",
    });

    engineMock.mockResolvedValueOnce({
      id: 3,
      title: "The Left Hand of Darkness",
      author: "Ursula K. Le Guin",
      rating: null,
      read_at: "2026-10-01",
      source: "manual",
      created_at: "2026-10-01T12:00:00Z",
      job_id: "job-score-1",
    });

    const clearResponse = await PUT({
      request: new Request("http://bookward.test/api/reading-history", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: 3, rating: null }),
      }),
    } as never);

    expect(clearResponse.status).toBe(200);
    expect(engineMock).toHaveBeenLastCalledWith("/api/reading-history/3", {
      method: "PUT",
      body: JSON.stringify({ rating: null }),
    });
    expect(await clearResponse.json()).toEqual({
      item: expect.objectContaining({ id: 3, rating: null }),
      job_id: "job-score-1",
    });
  });

  it("supports PUT on /api/reading-history/[id] and rejects invalid IDs or missing rating keys", async () => {
    engineMock.mockResolvedValue({
      id: 12,
      title: "Piranesi",
      author: "Susanna Clarke",
      rating: 4,
      read_at: null,
      source: "goodreads_csv",
      created_at: "2026-09-10T00:00:00Z",
      job_id: "job-score-2",
    });

    const response = await PUT_BY_ID({
      params: { id: "12" },
      request: new Request("http://bookward.test/api/reading-history/12", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rating: "4" }),
      }),
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith("/api/reading-history/12", {
      method: "PUT",
      body: JSON.stringify({ rating: 4 }),
    });

    const missingKeyResponse = await PUT_BY_ID({
      params: { id: "12" },
      request: new Request("http://bookward.test/api/reading-history/12", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      }),
    } as never);
    expect(missingKeyResponse.status).toBe(400);

    const badIdResponse = await PUT_BY_ID({
      params: { id: "0" },
      request: new Request("http://bookward.test/api/reading-history/0", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rating: 3 }),
      }),
    } as never);
    expect(badIdResponse.status).toBe(400);
    expect(engineMock).toHaveBeenCalledTimes(1);
  });

  it("rejects missing rating keys, non-integers, or out-of-range ratings before contacting the engine", async () => {
    for (const badBody of [
      { id: 3 },
      { id: 3, rating: 0 },
      { id: 3, rating: 6 },
      { id: 3, rating: 3.5 },
      { id: 3, rating: true },
      { id: 0, rating: 4 },
    ]) {
      const response = await PUT({
        request: new Request("http://bookward.test/api/reading-history", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(badBody),
        }),
      } as never);
      expect(response.status).toBe(400);
    }
    expect(engineMock).not.toHaveBeenCalled();
  });

  it("forwards upstream engine errors and statuses for client rollback", async () => {
    engineMock.mockRejectedValue(new EngineError("Read entry not found", 404));

    const response = await PUT({
      request: new Request("http://bookward.test/api/reading-history", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: 999, rating: 5 }),
      }),
    } as never);

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ message: "Read entry not found" });
  });
});
