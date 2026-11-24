import { afterEach, describe, expect, it, vi } from "vitest";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

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
  engine: engineMock,
}));

import { GET } from "./recommendations/+server";

afterEach(() => vi.clearAllMocks());

const PAGE_BOOK_KEYS = [
  "author",
  "cover_url",
  "explanation",
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
  it("loads the requested page without a device cookie and maps the response for the browser", async () => {
    engineMock.mockResolvedValue([
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
    ]);

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=recommended&limit=2&offset=4",
      ),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=recommended&limit=3&offset=4",
    );
    expect(Object.keys(payload.items[0]).sort()).toEqual(PAGE_BOOK_KEYS);
    expect(payload).toEqual({
      items: [
        {
          id: 7,
          recommendation_run_id: "",
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
          explanation: ["A good match"],
          reason: "A good match",
          status: "recommended",
        },
      ],
      has_more: false,
    });
  });

  it("sanitizes non-HTTP URLs and joins multiple explanation items", async () => {
    engineMock.mockResolvedValue([
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
    ]);

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
      explanation: ["Similar to a 5-star read", "Trusted source"],
      reason: "Similar to a 5-star read · Trusted source",
      status: "recommended",
    });
  });

  it("rejects oversized browser pages before contacting the engine", async () => {
    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?limit=25"),
    } as never);

    expect(response.status).toBe(400);
    expect(engineMock).not.toHaveBeenCalled();
  });

  it("maps shortlist books without reading-progress fields", async () => {
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
      },
      {
        id: 9,
        title: "An Imported Book",
        author: "A Reader",
        description: "",
        cover_url: "",
        source_url: "",
        release_date: null,
        date_kind: "unknown",
        genres: [],
        score: 70,
        explanation: [],
        status: "imported",
        source_name: "A source",
      },
    ]);

    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?status=saved"),
      cookies: { get: vi.fn() },
    } as never);

    expect(response.status).toBe(200);
    expect((await response.json()).items).toEqual([
      expect.objectContaining({ id: 8, status: "saved" }),
      expect.objectContaining({ id: 9, status: "imported" }),
    ]);
  });

  it("allows filtering shortlist books by status=imported", async () => {
    engineMock.mockResolvedValue([
      {
        id: 9,
        title: "An Imported Book",
        author: "A Reader",
        description: "",
        cover_url: "",
        source_url: "",
        release_date: null,
        date_kind: "unknown",
        genres: [],
        score: 70,
        explanation: [],
        status: "imported",
        source_name: "A source",
      },
    ]);

    const response = await GET({
      url: new URL("http://bookward.test/api/recommendations?status=imported&limit=8&offset=0"),
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=imported&limit=9&offset=0",
    );
    expect((await response.json()).items).toEqual([
      expect.objectContaining({ id: 9, status: "imported" }),
    ]);
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
      "/api/recommendations?status=decisions&limit=9&offset=0",
    );
  });

  it("reports no next page when a full decisions page is terminal", async () => {
    engineMock.mockResolvedValue(
      Array.from({ length: 24 }, (_, index) => recommendation(index + 1)),
    );

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=24&offset=24",
      ),
    } as never);
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/recommendations?status=decisions&limit=25&offset=24",
    );
    expect(payload.items).toHaveLength(24);
    expect(payload.has_more).toBe(false);
  });

  it("trims the extra row and reports another decisions page", async () => {
    engineMock.mockResolvedValue(
      Array.from({ length: 25 }, (_, index) => recommendation(index + 1)),
    );

    const response = await GET({
      url: new URL(
        "http://bookward.test/api/recommendations?status=decisions&limit=24&offset=24",
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
});
