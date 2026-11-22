import { afterEach, describe, expect, it, vi } from "vitest";

const { engineWithHeadersMock } = vi.hoisted(() => ({
  engineWithHeadersMock: vi.fn(),
}));

const recommendation = (id: number) => ({
  id,
  title: `Book ${id}`,
  author: "A Writer",
  description: "",
  cover_url: "",
  source_url: "",
  release_date: null,
  date_kind: "unknown",
  genres: [],
  score: 80,
  explanation: [],
  status: "rejected",
  source_name: "A source",
});

vi.mock("#lib/server/engine.js", async (importOriginal) => ({
  ...await importOriginal<typeof import("#lib/server/engine.js")>(),
  engineWithHeaders: engineWithHeadersMock,
}));

import { GET } from "./recommendations/+server";

afterEach(() => vi.clearAllMocks());

describe("recommendations endpoint", () => {
  it("loads the exact requested page without lookahead and maps the response for the browser", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [
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
          recommendation_run_id: "run-page-2",
          source_name: "A source",
        },
      ],
      headers: new Headers({
        "x-bookward-has-more": "false",
        "x-bookward-recommendation-run": "run-page-2",
      }),
    });

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=8&offset=24",
      ),
    } as never);

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=8&offset=24",
    );
    expect(response.headers.get("X-Bookward-Has-More")).toBe("false");
    expect(response.headers.get("X-Bookward-Recommendation-Run")).toBe("run-page-2");
    expect(await response.json()).toEqual({
      items: [
        expect.objectContaining({
          id: 7,
          cover_url: "https://covers.example/book.jpg",
          source_url: "https://books.example/book",
          reason: "A good match",
          published_on: "2026-10-01",
          recommendation_run_id: "run-page-2",
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
    expect(engineWithHeadersMock).not.toHaveBeenCalled();
  });

  it("maps shortlist books without reading-progress fields or run headers", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [
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
        },
      ],
      headers: new Headers({ "x-bookward-has-more": "false" }),
    });

    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?status=saved"),
      cookies: { get: vi.fn() },
    } as never);

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=saved&limit=8&offset=0",
    );
    expect(response.headers.get("X-Bookward-Recommendation-Run")).toBeNull();
    expect((await response.json()).items[0]).toMatchObject({
      id: 8,
      status: "saved",
      recommendation_run_id: "",
    });
  });

  it("allows the browser to request the past decisions archive with exact limit", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [],
      headers: new Headers({ "x-bookward-has-more": "false" }),
    });
    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=8&offset=0",
      ),
    } as never);

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=decisions&limit=8&offset=0",
    );
  });

  it("reports no next page when X-Bookward-Has-More is false on a full page", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: Array.from({ length: 24 }, (_, index) => recommendation(index + 1)),
      headers: new Headers({ "x-bookward-has-more": "false" }),
    });

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=24&offset=24",
      ),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=decisions&limit=24&offset=24",
    );
    expect(payload.items).toHaveLength(24);
    expect(payload.has_more).toBe(false);
  });

  it("reports another page when X-Bookward-Has-More is true without requesting an extra row", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: Array.from({ length: 24 }, (_, index) => recommendation(index + 1)),
      headers: new Headers({ "x-bookward-has-more": "true" }),
    });

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=24&offset=24",
      ),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=decisions&limit=24&offset=24",
    );
    expect(payload.items).toHaveLength(24);
    expect(payload.items.map((item: { id: number }) => item.id)).toEqual(
      Array.from({ length: 24 }, (_, index) => index + 1),
    );
    expect(payload.has_more).toBe(true);
  });
});
