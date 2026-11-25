import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineError } from "#lib/server/engine.js";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

vi.mock("#lib/server/engine.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("#lib/server/engine.js")>()),
  engine: engineMock,
}));

import * as collectionRoute from "./reading-history/+server";
import { PUT } from "./reading-history/[id]/+server";

afterEach(() => vi.clearAllMocks());

describe("reading-history endpoints", () => {
  it("loads reading history items via GET on /api/reading-history and exposes no collection write route", async () => {
    expect("PUT" in collectionRoute).toBe(false);

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

    const response = await collectionRoute.GET({} as never);
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

  it("updates and clears ratings via PUT on /api/reading-history/[id]", async () => {
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
      params: { id: "3" },
      request: new Request("http://bookward.test/api/reading-history/3", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rating: 5 }),
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
      params: { id: "3" },
      request: new Request("http://bookward.test/api/reading-history/3", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rating: null }),
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

  it("coerces valid digit strings and rejects invalid IDs, missing rating keys, empty strings, or out-of-range ratings with 400", async () => {
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

    const response = await PUT({
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

    for (const badBody of [
      {},
      { rating: "" },
      { rating: "   " },
      { rating: 0 },
      { rating: 6 },
      { rating: 3.5 },
      { rating: true },
      { rating: "five" },
    ]) {
      const badRatingResponse = await PUT({
        params: { id: "12" },
        request: new Request("http://bookward.test/api/reading-history/12", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(badBody),
        }),
      } as never);
      expect(badRatingResponse.status).toBe(400);
    }

    for (const badId of ["0", "-3", "abc"]) {
      const badIdResponse = await PUT({
        params: { id: badId },
        request: new Request(`http://bookward.test/api/reading-history/${badId}`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ rating: 3 }),
        }),
      } as never);
      expect(badIdResponse.status).toBe(400);
    }

    expect(engineMock).toHaveBeenCalledTimes(1);
  });

  it("forwards upstream engine errors and statuses for client rollback", async () => {
    engineMock.mockRejectedValue(new EngineError("Read entry not found", 404));

    const response = await PUT({
      params: { id: "999" },
      request: new Request("http://bookward.test/api/reading-history/999", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rating: 5 }),
      }),
    } as never);

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ message: "Read entry not found" });
  });

  it("updates and clears completion dates via PUT on /api/reading-history/[id] and rejects blank or future dates", async () => {
    engineMock.mockResolvedValueOnce({
      id: 15,
      title: "Station Eleven",
      author: "Emily St. John Mandel",
      rating: 4,
      read_at: "2025-11-04",
      source: "manual",
      created_at: "2025-11-04T00:00:00Z",
      job_id: "job-score-3",
    });

    const updateDateResponse = await PUT({
      params: { id: "15" },
      request: new Request("http://bookward.test/api/reading-history/15", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ read_at: "2025-11-04" }),
      }),
    } as never);

    expect(updateDateResponse.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith("/api/reading-history/15", {
      method: "PUT",
      body: JSON.stringify({ read_at: "2025-11-04" }),
    });
    expect(await updateDateResponse.json()).toEqual({
      item: expect.objectContaining({ id: 15, read_at: "2025-11-04", source: "manual" }),
      job_id: "job-score-3",
    });

    engineMock.mockResolvedValueOnce({
      id: 15,
      title: "Station Eleven",
      author: "Emily St. John Mandel",
      rating: 4,
      read_at: null,
      source: "manual_history",
      created_at: "2025-11-04T00:00:00Z",
      job_id: "job-score-4",
    });

    const clearDateResponse = await PUT({
      params: { id: "15" },
      request: new Request("http://bookward.test/api/reading-history/15", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ read_at: null }),
      }),
    } as never);

    expect(clearDateResponse.status).toBe(200);
    expect(engineMock).toHaveBeenLastCalledWith("/api/reading-history/15", {
      method: "PUT",
      body: JSON.stringify({ read_at: null }),
    });
    expect(await clearDateResponse.json()).toEqual({
      item: expect.objectContaining({ id: 15, read_at: null, source: "manual_history" }),
      job_id: "job-score-4",
    });

    for (const badDateBody of [
      { read_at: "" },
      { read_at: "   " },
      { read_at: "not-a-date" },
      { read_at: "2025-02-30" },
      { read_at: "2099-01-01" },
      { read_at: 12345 },
    ]) {
      const badDateResponse = await PUT({
        params: { id: "15" },
        request: new Request("http://bookward.test/api/reading-history/15", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(badDateBody),
        }),
      } as never);
      expect(badDateResponse.status).toBe(400);
    }

    expect(engineMock).toHaveBeenCalledTimes(2);
  });
});
