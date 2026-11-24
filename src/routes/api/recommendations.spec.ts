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

const PAGE_BOOK_KEYS = [
  "author",
  "cover_url",
  "genres",
  "id",
  "metadata_confidence",
  "published_kind",
  "published_on",
  "reason",
  "recommendation_run_id",
  "score",
  "source_url",
  "status",
  "synopsis",
  "title",
];

describe("recommendations endpoint", () => {
  it("loads the exact requested page without lookahead and maps explicit UI fields for the browser", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [
        {
          id: 7,
          title: "A Book",
          author: "A Writer",
          description: "A description",
          cover_url: "https://covers.example/book.jpg",
          source_url: "https://books.example/book",
          source_id: 3,
          release_date: "2026-10-01",
          date_kind: "day",
          genres: ["Fantasy"],
          score: 91,
          explanation: ["A good match"],
          status: "recommended",
          recommendation_run_id: "run-page-2",
          normalized_key: "a-book::a-writer",
          created_at: "2026-09-01 00:00:00",
          updated_at: "2026-09-02 00:00:00",
          isbn13: "9780000000007",
          isbn10: "0000000007",
          score_batch_id: "batch-7",
          source_name: "A source",
          quality_work_id: "/works/OL7W",
          quality_provider: "openlibrary",
          quality_isbn13: "9780000000007",
          quality_isbn10: "0000000007",
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
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=8&offset=24",
    );
    expect(response.headers.get("X-Bookward-Has-More")).toBe("false");
    expect(response.headers.get("X-Bookward-Recommendation-Run")).toBe("run-page-2");
    expect(Object.keys(payload.items[0]).sort()).toEqual(PAGE_BOOK_KEYS);
    expect(payload).toEqual({
      items: [
        {
          id: 7,
          recommendation_run_id: "run-page-2",
          title: "A Book",
          author: "A Writer",
          cover_url: "https://covers.example/book.jpg",
          source_url: "https://books.example/book",
          published_on: "2026-10-01",
          published_kind: "day",
          genres: ["Fantasy"],
          score: 91,
          metadata_confidence: null,
          synopsis: "A description",
          reason: "A good match",
          status: "recommended",
        },
      ],
      has_more: false,
    });
  });

  it("sanitizes non-HTTP URLs and joins multiple explanation items", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [
        {
          id: 11,
          recommendation_run_id: "run-abcd1234",
          title: "Unsafe Link Book",
          author: "A Writer",
          description: "Summary text",
          cover_url: "javascript:alert(1)",
          source_url: "ftp://files.example/book",
          release_date: null,
          date_kind: "unknown",
          genres: ["Mystery"],
          score: 77,
          metadata_confidence: 0.52,
          explanation: ["Similar to a 5-star read", "Trusted source"],
          status: "recommended",
          source_name: "A source",
        },
      ],
      headers: new Headers({
        "x-bookward-has-more": "false",
        "x-bookward-recommendation-run": "run-abcd1234",
      }),
    });

    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?status=recommended&limit=1"),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(Object.keys(payload.items[0]).sort()).toEqual(PAGE_BOOK_KEYS);
    expect(payload.items[0]).toEqual({
      id: 11,
      recommendation_run_id: "run-abcd1234",
      title: "Unsafe Link Book",
      author: "A Writer",
      cover_url: "",
      source_url: "",
      published_on: null,
      published_kind: "unknown",
      genres: ["Mystery"],
      score: 77,
      metadata_confidence: 0.52,
      synopsis: "Summary text",
      reason: "Similar to a 5-star read · Trusted source",
      status: "recommended",
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

  it("defensively trims items to limit if an upstream response returns extra rows", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: Array.from({ length: 25 }, (_, index) => recommendation(index + 1)),
      headers: new Headers({ "x-bookward-has-more": "true" }),
    });

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=24&offset=0",
      ),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(payload.items).toHaveLength(24);
    expect(payload.items.map((item: { id: number }) => item.id)).toEqual(
      Array.from({ length: 24 }, (_, index) => index + 1),
    );
    expect(payload.has_more).toBe(true);
  });

  it("falls back to page-size comparison when X-Bookward-Has-More header is absent", async () => {
    engineWithHeadersMock.mockResolvedValueOnce({
      data: Array.from({ length: 8 }, (_, index) => recommendation(index + 1)),
      headers: new Headers(),
    });

    const fullPage = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=8&offset=24",
      ),
    } as never);
    const fullPayload = await fullPage.json();
    expect(fullPage.status).toBe(200);
    expect(fullPage.headers.get("X-Bookward-Has-More")).toBeNull();
    expect(fullPayload.items).toHaveLength(8);
    expect(fullPayload.has_more).toBe(true);

    engineWithHeadersMock.mockResolvedValueOnce({
      data: Array.from({ length: 3 }, (_, index) => recommendation(index + 1)),
      headers: new Headers(),
    });

    const partialPage = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=8&offset=32",
      ),
    } as never);
    const partialPayload = await partialPage.json();
    expect(partialPage.status).toBe(200);
    expect(partialPage.headers.get("X-Bookward-Has-More")).toBeNull();
    expect(partialPayload.items).toHaveLength(3);
    expect(partialPayload.has_more).toBe(false);
  });

  it("forwards valid seed_run_id to the engine and rejects malformed seed_run_id", async () => {
    engineWithHeadersMock.mockResolvedValue({
      data: [],
      headers: new Headers({ "x-bookward-has-more": "false" }),
    });

    const okResponse = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=8&offset=24&seed_run_id=run-root-1234",
      ),
    } as never);
    expect(okResponse.status).toBe(200);
    expect(engineWithHeadersMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=8&offset=24&seed_run_id=run-root-1234",
    );

    engineWithHeadersMock.mockClear();
    const badResponse = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=8&offset=24&seed_run_id=bad",
      ),
    } as never);
    expect(badResponse.status).toBe(400);
    expect(engineWithHeadersMock).not.toHaveBeenCalled();
  });
});
