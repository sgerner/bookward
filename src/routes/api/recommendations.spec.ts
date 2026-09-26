import { afterEach, describe, expect, it, vi } from "vitest";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

vi.mock("$lib/server/engine", () => ({
  engine: engineMock,
  EngineError: class EngineError extends Error {
    status = 502;
  },
}));

import { GET } from "./recommendations/+server";

afterEach(() => vi.clearAllMocks());

describe("recommendations endpoint", () => {
  it("loads the requested page without a device cookie and maps the response for the browser", async () => {
    engineMock.mockResolvedValue([
      {
        id: 7,
        title: "A Book",
        author: "A Writer",
        description: "A description",
        cover_url: "https://covers.example/book.jpg",
        source_url: "https://books.example/book",
        release_date: "2026-10-01",
        date_kind: "day",
        genres: ["Fantasy"],
        score: 91,
        explanation: ["A good match"],
        status: "recommended",
        source_name: "A source",
        reading_status: null,
        up_next: 0,
      },
    ]);

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=2&offset=4",
      ),
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=2&offset=4",
    );
    expect(await response.json()).toEqual({
      items: [
        expect.objectContaining({
          id: 7,
          cover_url: "https://covers.example/book.jpg",
          source_url: "https://books.example/book",
          reason: "A good match",
          published_on: "2026-10-01",
          reading_status: null,
          up_next: 0,
        }),
      ],
      has_more: false,
    });
  });

  it("rejects oversized browser pages before contacting the engine", async () => {
    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?limit=25"),
    } as never);

    expect(response.status).toBe(400);
    expect(engineMock).not.toHaveBeenCalled();
  });

  it("keeps persisted reading progress when mapping shortlist books", async () => {
    engineMock.mockResolvedValue([
      {
        id: 8,
        title: "A Saved Book",
        author: "A Reader",
        description: "",
        cover_url: "",
        source_url: "",
        release_date: null,
        date_kind: "unknown",
        genres: [],
        score: 72,
        explanation: [],
        status: "saved",
        source_name: "A source",
        reading_status: "reading",
        up_next: 0,
        reading_rating: null,
        started_at: "2026-09-24 10:00:00",
        finished_at: null,
      },
    ]);

    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?status=saved"),
      cookies: { get: vi.fn() },
    } as never);

    expect(response.status).toBe(200);
    expect((await response.json()).items[0]).toMatchObject({
      id: 8,
      status: "saved",
      reading_status: "reading",
      started_at: "2026-09-24 10:00:00",
    });
  });

  it("allows the browser to request the past decisions archive", async () => {
    engineMock.mockResolvedValue([]);
    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=8&offset=0",
      ),
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=decisions&limit=8&offset=0",
    );
  });
});
