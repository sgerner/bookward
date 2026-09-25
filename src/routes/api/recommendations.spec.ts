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
  it("forwards the requested page and maps the response for the browser", async () => {
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
      },
    ]);

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=2&offset=4",
      ),
      cookies: { get: vi.fn().mockReturnValue("session-1234") },
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=2&offset=4",
      { headers: { "x-bookward-session": "session-1234" } },
    );
    expect(await response.json()).toEqual({
      items: [
        expect.objectContaining({
          id: 7,
          cover_url: "https://covers.example/book.jpg",
          source_url: "https://books.example/book",
          reason: "A good match",
          published_on: "2026-10-01",
        }),
      ],
      has_more: false,
    });
  });

  it("rejects oversized browser pages before contacting the engine", async () => {
    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?limit=25"),
      cookies: { get: vi.fn() },
    } as never);

    expect(response.status).toBe(400);
    expect(engineMock).not.toHaveBeenCalled();
  });
});
