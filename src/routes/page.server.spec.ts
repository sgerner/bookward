import { isHttpError } from "@sveltejs/kit";
import { afterEach, describe, expect, it, vi } from "vitest";
import { actions, load } from "./+page.server";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("page actions", () => {
  it("keeps the Settings view compatible with an engine before the token migration", async () => {
    const overview = {
      recommendations: [],
      history: [],
      sources: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "",
        librarr_api_key_set: false,
        librarr_media_type: "audiobook",
        digest: {},
      },
    };
    const fetchMock = vi.fn().mockImplementation(
      () => new Response(JSON.stringify(overview), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = (await load({
      url: new URL("http://afterword.test/?view=settings"),
      locals: { user: null },
    } as never)) as { profile: { api_tokens: unknown[] } };
    expect(result.profile.api_tokens).toEqual([]);
  });

  it("loads recommendations without creating or forwarding a device cookie", async () => {
    const pageBookKeys = [
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
    const overview = {
          recommendation_run_id: "run-1234",
          recommendations: [
            {
              id: 5,
              title: "Recommended book",
              author: "First Writer",
              description: "Recommended summary",
              cover_url: "https://covers.example/recommended.jpg",
              source_url: "https://books.example/recommended",
              source_id: 1,
              release_date: "2026-11-15",
              date_kind: "day",
              genres: ["Science Fiction"],
              score: 94,
              metadata_confidence: 0.92,
              explanation: ["Matches rated books", "Trusted list"],
              status: "recommended",
              normalized_key: "recommended-book::first-writer",
              created_at: "2026-10-01 00:00:00",
              updated_at: "2026-10-02 00:00:00",
              isbn13: "9780000000005",
              isbn10: "0000000005",
              score_batch_id: "batch-5",
              source_name: "A source",
              quality_work_id: "/works/OL5W",
              quality_provider: "openlibrary",
              quality_isbn13: "9780000000005",
              quality_isbn10: "0000000005",
            },
          ],
          decisions: [
            {
              id: 9,
              title: "Deferred book",
              author: "A Writer",
              description: "A description",
              cover_url: "https://covers.example/deferred.jpg",
              source_url: "https://books.example/deferred",
              source_id: 2,
              release_date: null,
              date_kind: "unknown",
              genres: ["Fantasy"],
              score: 81,
              metadata_confidence: 0.8,
              explanation: ["A good match"],
              status: "maybe_later",
              normalized_key: "deferred-book::a-writer",
              created_at: "2026-09-01 00:00:00",
              updated_at: "2026-09-02 00:00:00",
              isbn13: "9780000000009",
              isbn10: "0000000009",
              score_batch_id: "batch-9",
              source_name: "A source",
              quality_work_id: "/works/OL9W",
              quality_provider: "openlibrary",
              quality_isbn13: "9780000000009",
              quality_isbn10: "0000000009",
            },
          ],
          history: [],
          sources: [],
          settings: {
            embedding_backend: "local",
            embedding_model: "hashing-768",
            embedding_url: "",
            embedding_api_key_set: false,
            librarr_url: "",
            librarr_api_key_set: false,
            librarr_media_type: "audiobook",
            digest: {},
          },
    };
    const fetchMock = vi.fn().mockImplementation(
      () => new Response(JSON.stringify(overview), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = (await load({
      url: new URL("http://afterword.test/"),
      locals: { user: null },
    } as never)) as {
      recommendation_run_id: string;
      books: Array<Record<string, unknown>>;
      decisions: Array<Record<string, unknown>>;
    };
    expect(result.recommendation_run_id).toBe("run-1234");
    expect(Object.keys(result.books[0]).sort()).toEqual(pageBookKeys);
    expect(result.books).toEqual([
      {
        id: 5,
        recommendation_run_id: "run-1234",
        title: "Recommended book",
        author: "First Writer",
        cover_url: "https://covers.example/recommended.jpg",
        source_url: "https://books.example/recommended",
        published_on: "2026-11-15",
        published_kind: "day",
        genres: ["Science Fiction"],
        score: 94,
        metadata_confidence: 0.92,
        synopsis: "Recommended summary",
        explanation: ["Matches rated books", "Trusted list"],
        reason: "Matches rated books · Trusted list",
        status: "recommended",
      },
    ]);
    expect(Object.keys(result.decisions[0]).sort()).toEqual(pageBookKeys);
    expect(result.decisions).toEqual([
      {
        id: 9,
        recommendation_run_id: "",
        title: "Deferred book",
        author: "A Writer",
        cover_url: "https://covers.example/deferred.jpg",
        source_url: "https://books.example/deferred",
        published_on: null,
        published_kind: "unknown",
        genres: ["Fantasy"],
        score: 81,
        metadata_confidence: 0.8,
        synopsis: "A description",
        explanation: ["A good match"],
        reason: "A good match",
        status: "maybe_later",
      },
    ]);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/overview?recommendation_limit=24");
    expect(fetchMock.mock.calls[0][1]?.headers).not.toHaveProperty("x-bookward-session");
  });

  it("throws a 503 string error without deprecation warnings when the engine is unavailable", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchMock = vi
      .fn()
      .mockRejectedValue(new Error("connect ECONNREFUSED 127.0.0.1:8000"));
    vi.stubGlobal("fetch", fetchMock);

    let thrown: unknown;
    try {
      await load({
        url: new URL("http://afterword.test/"),
        locals: { user: null },
      } as never);
    } catch (error) {
      thrown = error;
    }

    expect(isHttpError(thrown, 503)).toBe(true);
    if (isHttpError(thrown, 503)) {
      expect(thrown.body.message).toBe(
        "Bookward's recommendation engine is unavailable.",
      );
    }
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("marks a recommended book as read with an optional rating and no device session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "read", rating: 5 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "42");
    body.set("rating", "5");
    const result = await actions.markRead!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({
      message: "Marked Read and added to your history with a 5-star rating.",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/recommendations/42/read"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ rating: 5 }),
      }),
    );
  });

  it("rejects an invalid manual read rating without contacting the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "42");
    body.set("rating", "6");
    const result = await actions.markRead!({
      request: new Request("http://afterword.test", { method: "POST", body }),
      cookies: { get: vi.fn() },
    } as never);

    expect(result).toMatchObject({ status: 400 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates and revokes API tokens through the engine actions", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ token: "bkw_one-time-secret" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ revoked: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const createBody = new FormData();
    createBody.set("name", "Home Assistant");
    await expect(
      actions.createApiToken!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: createBody,
        }),
      } as never),
    ).resolves.toEqual({
      message: "API token created. Copy it now; it will not be shown again.",
      token: "bkw_one-time-secret",
    });

    const revokeBody = new FormData();
    revokeBody.set("id", "4");
    await expect(
      actions.revokeApiToken!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: revokeBody,
        }),
      } as never),
    ).resolves.toEqual({ message: "API token revoked." });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/settings/api-tokens",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/settings/api-tokens/4",
    );
  });

  it("maps shortlist decisions to the engine feedback contract", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ id: 7, status: "saved" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "7");
    body.set("status", "saved");
    const result = await actions.decide!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "Saved to your shortlist." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/recommendations/7/feedback",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ action: "save" }),
      }),
    );
  });

  it("forwards the recommendation run with explicit feedback", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 7, status: "saved" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "7");
    body.set("status", "saved");
    body.set("run_id", "run-1234");
    await actions.decide!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      action: "save",
      run_id: "run-1234",
    });
  });

  it("sends Maybe later as a neutral decision and exposes its undo token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 9, status: "maybe_later", decision_id: 55 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "9");
    body.set("status", "maybe_later");
    const result = await actions.decide!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({
      message: "Set aside for later.",
      undo_id: 55,
      id: 9,
      undo_label: "setting this book aside",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      action: "maybe_later",
    });
  });

  it("forwards an Undo request with the server-issued decision ID", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 9, status: "recommended" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "9");
    body.set("decision_id", "55");
    const result = await actions.undoDecision!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({ message: "Decision undone." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/recommendations/9/undo",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision_id: 55 }),
      }),
    );
  });

  it("exposes a restore Undo token when returning a book to Discover", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 9, status: "recommended", decision_id: 61 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "9");
    body.set("status", "recommended");
    const result = await actions.decide!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({
      message: "Returned to Discover.",
      undo_id: 61,
      id: 9,
      undo_label: "restoring this book",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      action: "restore",
    });
  });

  it("routes multi-item Undo requests to bulk-undo and propagates 409 conflicts", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ undone: 2 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Only the latest decision can be undone" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.append("id", "7");
    body.append("decision_id", "21");
    body.append("id", "8");
    body.append("decision_id", "22");
    const result = await actions.undoDecision!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({ message: "Decisions undone." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/recommendations/bulk-undo",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          decisions: [
            { id: 7, decision_id: 21 },
            { id: 8, decision_id: 22 },
          ],
        }),
      }),
    );

    const conflict = (await actions.undoDecision!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never)) as { status: number; data: { message: string; error: boolean } };
    expect(conflict.status).toBe(409);
    expect(conflict.data).toEqual({
      message: "Only the latest decision can be undone",
      error: true,
    });

    const invalid = new FormData();
    invalid.append("id", "7");
    invalid.append("id", "8");
    invalid.append("decision_id", "21");
    const badRequest = (await actions.undoDecision!({
      request: new Request("http://afterword.test", { method: "POST", body: invalid }),
    } as never)) as { status: number; data: { message: string } };
    expect(badRequest.status).toBe(400);
  });

  it("rejects an invalid URL when adding a custom source without contacting the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    for (const invalidUrl of [
      "not-a-url",
      "",
      null,
      new File(["https://books.example/list"], "url.txt"),
    ]) {
      const body = new FormData();
      body.set("label", "Invalid source");
      if (invalidUrl !== null) body.set("url", invalidUrl);

      const result = await actions.source!({
        request: new Request("http://afterword.test", { method: "POST", body }),
      } as never);

      expect(result).toMatchObject({
        status: 400,
        data: { message: "Add a valid name and public URL.", error: true },
      });
    }

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not save a custom source when preview finds no books", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ count: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("label", "Empty list");
    body.set("url", "https://books.example/list");
    const result = (await actions.source!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never)) as { status: number; data: { message: string } };
    expect(result.status).toBe(400);
    expect(result.data.message).toContain("No recognizable books");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("persists the resolved feed URL from preview while keeping the website hostname as default name", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            count: 3,
            url: "https://feeds.newsletter.example/books.atom",
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 12, job_id: 4 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("url", "https://www.newsletter.example");
    const result = await actions.source!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message:
        "Permanent source added; its first scan is queued (3 books detected).",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/sources/preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://www.newsletter.example",
          filters: { include_genres: [], exclude_genres: [] },
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/sources",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "newsletter.example",
          url: "https://feeds.newsletter.example/books.atom",
          lifecycle: "permanent",
          filters: { include_genres: [], exclude_genres: [] },
        }),
      }),
    );
  });

  it("saves normalized source genre filters after previewing matches", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 3, raw_count: 10 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ saved: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "7");
    body.set("url", "https://books.example/list");
    body.set("currentUrl", "https://books.example/list");
    body.set("includeGenres", " Science fiction, fantasy, science fiction ");
    body.set("excludeGenres", " romance ");
    const result = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message: "Source filters updated (3 books matched); a fresh scan is queued.",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/sources/preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://books.example/list",
          filters: {
            include_genres: ["Science fiction", "fantasy"],
            exclude_genres: ["romance"],
          },
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/sources/7",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          filters: {
            include_genres: ["Science fiction", "fantasy"],
            exclude_genres: ["romance"],
          },
        }),
      }),
    );
  });

  it("warns and requires confirmation before saving zero-match source filters", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 0, raw_count: 12 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 0, raw_count: 12 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ saved: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const unconfirmed = new FormData();
    unconfirmed.set("id", "7");
    unconfirmed.set("url", "https://books.example/list");
    unconfirmed.set("currentUrl", "https://books.example/list");
    unconfirmed.set("includeGenres", "cyberpunk");
    const warning = (await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: unconfirmed,
      }),
    } as never)) as {
      status: number;
      data: {
        message: string;
        zeroMatchConfirm?: { id: number; rawCount: number };
      };
    };
    expect(warning.status).toBe(400);
    expect(warning.data.message).toContain("0 of 12 books");
    expect(warning.data.zeroMatchConfirm).toMatchObject({ id: 7, rawCount: 12 });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const confirmed = new FormData();
    confirmed.set("id", "7");
    confirmed.set("url", "https://books.example/list");
    confirmed.set("currentUrl", "https://books.example/list");
    confirmed.set("includeGenres", "cyberpunk");
    confirmed.set("confirmZeroMatch", "1");
    const saved = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: confirmed,
      }),
    } as never);
    expect(saved).toEqual({
      message:
        "Zero-match source filters confirmed and saved; a fresh scan is queued.",
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("previews and updates custom source URLs and switches lifecycles both ways", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 5, raw_count: 6 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 5, raw_count: 6 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 7 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 7, lifecycle: "permanent" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const previewForm = new FormData();
    previewForm.set("id", "7");
    previewForm.set("url", "https://books.example/new-feed");
    previewForm.set("currentUrl", "https://books.example/old-feed");
    previewForm.set("intent", "preview");
    const previewResult = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: previewForm,
      }),
    } as never);
    expect(previewResult).toMatchObject({
      message: "Preview matched 5 of 6 books.",
      sourcePreview: { id: 7, url: "https://books.example/new-feed", count: 5, rawCount: 6 },
    });

    const saveForm = new FormData();
    saveForm.set("id", "7");
    saveForm.set("url", "https://books.example/new-feed");
    saveForm.set("currentUrl", "https://books.example/old-feed");
    saveForm.set("intent", "save");
    const saveResult = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: saveForm,
      }),
    } as never);
    expect(saveResult).toEqual({
      message: "Source updated (5 books matched); a fresh scan is queued.",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/sources/7",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          filters: { include_genres: [], exclude_genres: [] },
          url: "https://books.example/new-feed",
        }),
      }),
    );

    const permanentForm = new FormData();
    permanentForm.set("id", "7");
    const permanentResult = await actions.switchSourceToPermanent!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: permanentForm,
      }),
    } as never);
    expect(permanentResult).toEqual({
      message: "Source changed to a permanent feed; a fresh scan is queued.",
    });

    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 4, raw_count: 4 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 7, enabled: 0, job_id: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ id: 7, enabled: 0, lifecycle: "permanent", job_id: null }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );

    const disabledSaveForm = new FormData();
    disabledSaveForm.set("id", "7");
    disabledSaveForm.set("url", "https://books.example/disabled-feed");
    disabledSaveForm.set("currentUrl", "https://books.example/new-feed");
    disabledSaveForm.set("intent", "save");
    const disabledSaveResult = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: disabledSaveForm,
      }),
    } as never);
    expect(disabledSaveResult).toEqual({
      message: "Source updated (4 books matched).",
    });

    const disabledPermanentForm = new FormData();
    disabledPermanentForm.set("id", "7");
    const disabledPermanentResult = await actions.switchSourceToPermanent!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: disabledPermanentForm,
      }),
    } as never);
    expect(disabledPermanentResult).toEqual({
      message: "Source changed to a permanent feed.",
    });
  });

  it("validates Google Books key updates and reports queued metadata refreshes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "Google Books rejected the API key (HTTP 400: API key not valid.)",
          }),
          { status: 400, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            saved: true,
            google_books_api_key_set: true,
            validation_status: "deferred",
            validation_warning:
              "Google Books was temporarily unreachable; key saved without live verification.",
            job_ids: { metadata: 10, read_metadata: 11 },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            saved: true,
            google_books_api_key_set: true,
            validation_status: "verified",
            validation_warning: "",
            job_ids: { metadata: 11, read_metadata: 12 },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const badForm = new FormData();
    badForm.set("apiKey", "bad-key");
    const rejected = (await actions.configureGoogleBooks!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: badForm,
      }),
    } as never)) as { status: number; data: { message: string } };
    expect(rejected.status).toBe(400);
    expect(rejected.data.message).toContain("Google Books rejected the API key");

    const deferredForm = new FormData();
    deferredForm.set("apiKey", "deferred-google-key");
    const deferred = await actions.configureGoogleBooks!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: deferredForm,
      }),
    } as never);
    expect(deferred).toEqual({
      message:
        "Google Books was temporarily unreachable; key saved without live verification. Metadata refresh queued.",
    });

    const goodForm = new FormData();
    goodForm.set("apiKey", "valid-google-key");
    const accepted = await actions.configureGoogleBooks!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: goodForm,
      }),
    } as never);
    expect(accepted).toEqual({
      message: "Google Books API key verified and saved; metadata refresh queued.",
    });
  });

  it("saves the permanent-source refresh cadence", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ saved: true, interval_hours: 24 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("intervalHours", "24");
    const result = await actions.configureSourceSchedule!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message: "Permanent sources will be scanned daily (UTC).",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/sources/schedule",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ interval_hours: 24 }),
      }),
    );
  });

  it("preserves an existing Librarr key while changing the default format", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ saved: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("url", "http://librarr:5050");
    body.set("apiKey", "");
    body.set("mediaType", "ebook");
    const result = await actions.configureLibrar!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "Librarr connection saved for ebooks." });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/settings");
    expect(fetchMock.mock.calls[0][0]).not.toContain("/api/overview");
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({
      librarr_url: "http://librarr:5050",
      librarr_api_key: "",
      librarr_media_type: "ebook",
    });
  });

  it("queues an embedding rebuild and waits for its background job", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "job-123" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: '{"embeddings":8}',
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const result = await actions.rebuildEmbeddings!({} as never);
    expect(result).toEqual({
      message: "Embeddings rebuilt and recommendations refreshed.",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/embeddings/rebuild",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/jobs/job-123",
    );
  });

  it("returns 502 when an embedding rebuild job fails or throws", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "job-fail-1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "failed",
            error: "Embedder unreachable.",
            result: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "job-fail-2" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "failed",
            error: null,
            result: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockRejectedValueOnce(new Error("Connection refused"));
    vi.stubGlobal("fetch", fetchMock);

    const firstPromise = actions.rebuildEmbeddings!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(firstPromise).resolves.toMatchObject({
      status: 502,
      data: { message: "Embedder unreachable.", error: true },
    });

    const secondPromise = actions.rebuildEmbeddings!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(secondPromise).resolves.toMatchObject({
      status: 502,
      data: { message: "Embedding rebuild failed.", error: true },
    });

    await expect(actions.rebuildEmbeddings!({} as never)).resolves.toMatchObject({
      status: 502,
      data: { message: "Bookward's recommendation engine is unavailable.", error: true },
    });
  });

  it("stops polling an embedding rebuild after 20 attempts and reports it running", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/api/embeddings/rebuild")) {
        return Promise.resolve(
          new Response(JSON.stringify({ job_id: "job-1234567890" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: "running", error: null, result: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const pending = actions.rebuildEmbeddings!({} as never);
    await vi.advanceTimersByTimeAsync(20 * 500);
    const result = await pending;

    expect(result).toEqual({
      message: "Embedding rebuild is still running (job-1234).",
    });
    expect(fetchMock).toHaveBeenCalledTimes(21);
  });

  it("queues a sync job and waits for completion", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-123" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: '{"errors":[]}',
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const pending = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    const result = await pending;

    expect(result).toEqual({
      message: "Sources scanned and recommendations refreshed.",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/sync",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/jobs/sync-123",
    );
  });

  it("reports singular and plural source error counts when a sync completes with errors", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-err-1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: JSON.stringify({ errors: ["Feed timed out"] }),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-err-2" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: JSON.stringify({ errors: ["Feed timed out", "404 Not Found"] }),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const singlePromise = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(singlePromise).resolves.toEqual({
      message: "Sources refreshed with 1 source needing attention.",
    });

    const multiPromise = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(multiPromise).resolves.toEqual({
      message: "Sources refreshed with 2 sources needing attention.",
    });
  });

  it("ignores malformed JSON in a completed sync job result", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-bad-json" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: "{not-valid-json",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const pending = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(pending).resolves.toEqual({
      message: "Sources scanned and recommendations refreshed.",
    });
  });

  it("returns 502 when a sync job fails or throws", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-fail-1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "failed",
            error: "Database locked.",
            result: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "sync-fail-2" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "failed",
            error: null,
            result: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockRejectedValueOnce(new Error("Sync unreachable"));
    vi.stubGlobal("fetch", fetchMock);

    const firstPromise = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(firstPromise).resolves.toMatchObject({
      status: 502,
      data: { message: "Database locked.", error: true },
    });

    const secondPromise = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(500);
    await expect(secondPromise).resolves.toMatchObject({
      status: 502,
      data: { message: "Recommendation refresh failed.", error: true },
    });

    await expect(actions.runSync!({} as never)).resolves.toMatchObject({
      status: 502,
      data: { message: "Bookward's recommendation engine is unavailable.", error: true },
    });
  });

  it("stops polling a sync job after 20 attempts and reports it running", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/api/sync")) {
        return Promise.resolve(
          new Response(JSON.stringify({ job_id: "sync-123456" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: "running", error: null, result: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const pending = actions.runSync!({} as never);
    await vi.advanceTimersByTimeAsync(20 * 500);
    const result = await pending;

    expect(result).toEqual({
      message: "Refresh is still running (sync-123).",
    });
    expect(fetchMock).toHaveBeenCalledTimes(21);
  });

  it("persists digest timing and channel settings without sending blank secrets", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ saved: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("enabled", "on");
    body.append("channels", "discord");
    body.append("channels", "email");
    body.set("day", "2");
    body.set("time", "08:30");
    body.set("timezone", "America/Phoenix");
    body.set("minimumScore", "84");
    body.set("maximumBooks", "6");
    body.set("onlyNew", "on");
    body.set("appUrl", "https://afterword.example");
    body.set("emailTo", "reader@example.com");
    body.set("emailFrom", "afterword@example.com");
    body.set("smtpHost", "smtp.example.com");
    body.set("smtpPort", "587");
    body.set("smtpSecurity", "starttls");
    const result = await actions.configureDigest!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "Weekly digest settings saved." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/digest/settings",
      expect.objectContaining({
        method: "PUT",
        body: expect.not.stringContaining("discord_webhook_url"),
      }),
    );
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toMatchObject({
      enabled: true,
      channels: ["discord", "email"],
      day: 2,
      minimum_score: 84,
      maximum_books: 6,
      smtp_security: "starttls",
    });
  });

  it("retries a digest delivery when given a valid delivery UUID", async () => {
    const deliveryId = "550e8400-e29b-41d4-a716-446655440000";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ job_id: "job-456" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: '{"status":"sent"}',
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.set("id", deliveryId);
    const result = await actions.retryDigest!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({ message: "Digest delivery sent." });
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:8000/api/digest/deliveries/${deliveryId}/retry`,
      expect.objectContaining({
        method: "POST",
        body: "{}",
      }),
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/jobs/job-456",
    );
  });

  it("rejects an invalid delivery UUID in retryDigest without contacting the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    for (const invalidId of [
      "not-a-uuid",
      "",
      null,
      new File(["550e8400-e29b-41d4-a716-446655440000"], "id.txt"),
    ]) {
      const body = new FormData();
      if (invalidId !== null) body.set("id", invalidId);

      const result = await actions.retryDigest!({
        request: new Request("http://afterword.test", { method: "POST", body }),
      } as never);

      expect(result).toMatchObject({
        status: 400,
        data: { message: "Invalid delivery.", error: true },
      });
    }

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shortlists a selected digest batch in one engine call", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ updated: 2 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.append("ids", "7");
    body.append("ids", "8");
    const result = await actions.shortlistBulk!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "2 books added to your shortlist." });
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      ids: [7, 8],
      action: "save",
    });
  });

  it("saves keyless Ollama reranker settings with default local URL without calling models.dev", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          saved: true,
          llm_rerank: {
            enabled: true,
            mode: "ollama",
            provider: "ollama",
            api_url: "http://ollama:11434",
            model: "qwen2.5:7b",
            api_key_set: false,
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("enabled", "on");
    body.set("mode", "ollama");
    body.set("apiUrl", "");
    body.set("model", "qwen2.5:7b");
    const result = await actions.configureLlmRerank!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "LLM reranker verified and saved." });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/settings/llm-rerank",
    );
    expect(
      JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body)),
    ).toEqual({
      enabled: true,
      mode: "ollama",
      provider: "",
      api_url: "http://ollama:11434",
      model: "qwen2.5:7b",
      api_key: null,
      clear_api_key: false,
    });
  });

  it("saves keyless OpenAI-compatible reranker settings and surfaces 400 SSRF errors", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ saved: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "Private, loopback, and link-local addresses are not allowed",
          }),
          { status: 400, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const okBody = new FormData();
    okBody.set("enabled", "on");
    okBody.set("mode", "openai-compatible");
    okBody.set("apiUrl", "http://vllm:8000/v1");
    okBody.set("model", "Qwen/Qwen2.5-7B-Instruct");
    await expect(
      actions.configureLlmRerank!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: okBody,
        }),
      } as never),
    ).resolves.toEqual({ message: "LLM reranker verified and saved." });

    const blockedBody = new FormData();
    blockedBody.set("enabled", "on");
    blockedBody.set("mode", "openai-compatible");
    blockedBody.set("apiUrl", "http://127.0.0.1:8000/v1");
    blockedBody.set("model", "Qwen/Qwen2.5-7B-Instruct");
    const blocked = (await actions.configureLlmRerank!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: blockedBody,
      }),
    } as never)) as { status: number; data: { message: string } };
    expect(blocked.status).toBe(400);
    expect(blocked.data.message).toContain("Private, loopback");
  });

  it("throws HTTP 503 without contacting the engine when hooks report an outage", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      load({
        url: new URL("http://afterword.test/"),
        locals: { engineUnavailable: true },
      } as never),
    ).rejects.toMatchObject({
      status: 503,
      body: { message: "Bookward's recommendation engine is unavailable." },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws HTTP 503 without leaking internal error paths when overview fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("sqlite3.OperationalError: /tmp/secret/engine.db")),
    );

    let caught: unknown;
    try {
      await load({
        url: new URL("http://afterword.test/"),
        locals: { user: undefined },
      } as never);
    } catch (error) {
      caught = error;
    }

    expect(caught).toMatchObject({
      status: 503,
      body: { message: "Bookward's recommendation engine is unavailable." },
    });
    expect(JSON.stringify(caught)).not.toContain("engine.db");
  });

  it("renames a custom source without running preview when its URL is unchanged", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify([
            {
              id: 14,
              name: "goodreads.com",
              url: "https://books.example/feed.xml",
              enabled: 1,
              kind: "web",
              is_default: 0,
              is_custom: true,
              lifecycle: "permanent",
              filters: { include_genres: ["Sci-Fi"], exclude_genres: [] },
              last_status: "ok:4",
              last_scanned_at: "2026-10-01T00:00:00Z",
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 14,
            name: "Sci-Fi Shelf",
            url: "https://books.example/feed.xml",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.set("id", "14");
    body.set("label", "Sci-Fi Shelf");
    body.set("url", "https://books.example/feed.xml");
    const result = await actions.editSource!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({ message: "Source updated." });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/sources");
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8000/api/sources/14");
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({
      name: "Sci-Fi Shelf",
    });
  });

  it("previews a changed custom source URL before saving it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify([
            {
              id: 14,
              name: "goodreads.com",
              url: "https://books.example/old.xml",
              enabled: 1,
              kind: "web",
              is_default: 0,
              is_custom: true,
              lifecycle: "permanent",
              filters: { include_genres: ["Fantasy"], exclude_genres: ["Horror"] },
              last_status: "error:404",
              last_scanned_at: null,
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 3 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 14,
            name: "Moved Fantasy Feed",
            url: "https://books.example/new.xml",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.set("id", "14");
    body.set("label", "Moved Fantasy Feed");
    body.set("url", "https://books.example/new.xml");
    const result = await actions.editSource!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({
      message:
        "Source updated; a fresh scan is queued (3 books detected). Earlier books stay attributed to this source.",
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/sources/preview",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({
      url: "https://books.example/new.xml",
      filters: { include_genres: ["Fantasy"], exclude_genres: ["Horror"] },
    });
    expect(fetchMock.mock.calls[2][0]).toBe("http://127.0.0.1:8000/api/sources/14");
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({
      name: "Moved Fantasy Feed",
      url: "https://books.example/new.xml",
    });
  });

  it("forwards canonical preview.url when editing a custom source to a homepage and rejects canonical collisions", async () => {
    const sourcesPayload = [
      {
        id: 14,
        name: "Old Feed",
        url: "https://books.example/old.xml",
        enabled: 1,
        kind: "web",
        is_default: 0,
        is_custom: true,
        lifecycle: "permanent",
        filters: { include_genres: [], exclude_genres: [] },
        last_status: "ok:2",
        last_scanned_at: "2026-03-01T00:00:00Z",
      },
      {
        id: 15,
        name: "Existing Atom Feed",
        url: "https://feeds.existing.example/books.atom",
        enabled: 1,
        kind: "web",
        is_default: 0,
        is_custom: true,
        lifecycle: "permanent",
        filters: { include_genres: [], exclude_genres: [] },
        last_status: "ok:5",
        last_scanned_at: "2026-03-01T00:00:00Z",
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(sourcesPayload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            count: 4,
            url: "https://feeds.bookdispatches.example/books.atom",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 14,
            name: "Book Dispatches",
            url: "https://feeds.bookdispatches.example/books.atom",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(sourcesPayload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            count: 5,
            url: "https://feeds.existing.example/books.atom",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.set("id", "14");
    body.set("label", "Book Dispatches");
    body.set("url", "https://www.bookdispatches.example");
    const updated = await actions.editSource!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(updated).toEqual({
      message:
        "Source updated; a fresh scan is queued (4 books detected). Earlier books stay attributed to this source.",
    });
    expect(fetchMock.mock.calls[2][0]).toBe("http://127.0.0.1:8000/api/sources/14");
    expect(
      JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body)),
    ).toEqual({
      name: "Book Dispatches",
      url: "https://feeds.bookdispatches.example/books.atom",
    });

    const conflictBody = new FormData();
    conflictBody.set("id", "14");
    conflictBody.set("label", "Duplicate Homepage");
    conflictBody.set("url", "https://www.existing.example");
    const conflict = await actions.editSource!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: conflictBody,
      }),
    } as never);
    expect(conflict).toMatchObject({
      status: 409,
      data: {
        error: true,
        message: "A source with that URL already exists.",
        editSource: {
          id: 14,
          label: "Duplicate Homepage",
          url: "https://www.existing.example",
        },
      },
    });
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("preserves user input when custom source validation, conflict, or preview fails", async () => {
    const invalidBody = new FormData();
    invalidBody.set("id", "14");
    invalidBody.set("label", "A");
    invalidBody.set("url", "not-a-url");
    const invalidResult = await actions.editSource!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: invalidBody,
      }),
    } as never);
    expect(invalidResult).toMatchObject({
      status: 400,
      data: {
        error: true,
        message: "Add a valid name and public URL.",
        editSource: { id: 14, label: "A", url: "not-a-url" },
      },
    });

    const sourcesPayload = [
      {
        id: 14,
        name: "Feed A",
        url: "https://books.example/a.xml",
        enabled: 1,
        kind: "web",
        is_default: 0,
        is_custom: true,
        lifecycle: "permanent",
        filters: { include_genres: [], exclude_genres: [] },
        last_status: null,
        last_scanned_at: null,
      },
      {
        id: 15,
        name: "Feed B",
        url: "https://books.example/b.xml",
        enabled: 1,
        kind: "web",
        is_default: 0,
        is_custom: true,
        lifecycle: "permanent",
        filters: { include_genres: [], exclude_genres: [] },
        last_status: null,
        last_scanned_at: null,
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(sourcesPayload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(sourcesPayload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ count: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const duplicateBody = new FormData();
    duplicateBody.set("id", "14");
    duplicateBody.set("label", "Feed A Renamed");
    duplicateBody.set("url", "https://books.example/b.xml");
    const duplicateResult = await actions.editSource!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: duplicateBody,
      }),
    } as never);
    expect(duplicateResult).toMatchObject({
      status: 409,
      data: {
        error: true,
        message: "A source with that URL already exists.",
        editSource: {
          id: 14,
          label: "Feed A Renamed",
          url: "https://books.example/b.xml",
        },
      },
    });

    const emptyPreviewBody = new FormData();
    emptyPreviewBody.set("id", "14");
    emptyPreviewBody.set("label", "Feed A Empty");
    emptyPreviewBody.set("url", "https://books.example/empty.xml");
    const emptyPreviewResult = await actions.editSource!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: emptyPreviewBody,
      }),
    } as never);
    expect(emptyPreviewResult).toMatchObject({
      status: 400,
      data: {
        error: true,
        message: "No recognizable books were found at that URL.",
        editSource: {
          id: 14,
          label: "Feed A Empty",
          url: "https://books.example/empty.xml",
        },
      },
    });
  });

  it("switches source lifecycle in both directions and queues single-source refreshes", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ id: 14, job_id: "job-source-14" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const toOneTime = new FormData();
    toOneTime.set("id", "14");
    toOneTime.set("lifecycle", "one_time");
    await expect(
      actions.switchSourceLifecycle!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: toOneTime,
        }),
      } as never),
    ).resolves.toEqual({ message: "Source changed to a one-time import." });

    const toPermanent = new FormData();
    toPermanent.set("id", "14");
    toPermanent.set("lifecycle", "permanent");
    await expect(
      actions.switchSourceLifecycle!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: toPermanent,
        }),
      } as never),
    ).resolves.toEqual({ message: "Source restored to a permanent feed." });

    const refresh = new FormData();
    refresh.set("id", "14");
    await expect(
      actions.refreshSource!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: refresh,
        }),
      } as never),
    ).resolves.toEqual({ message: "Source refresh queued." });

    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      lifecycle: "one_time",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({
      lifecycle: "permanent",
    });
    expect(fetchMock.mock.calls[2][0]).toBe(
      "http://127.0.0.1:8000/api/sources/14/refresh",
    );
  });

  it("hydrates full Digest Review picks beyond the initial 24 Discover page and exposes full-pool genres", async () => {
    const overview = {
      recommendation_run_id: "run-9999",
      recommendations_has_more: true,
      recommendation_genres: ["Fantasy", "Xenolinguistics"],
      decision_genres: ["Historical Fiction", "Solarpunk"],
      recommendations: [
        {
          id: 1,
          title: "Top Ranked Pick",
          author: "Writer One",
          description: "In page one",
          cover_url: "https://covers.example/1.jpg",
          source_url: "https://books.example/1",
          release_date: "2026-09-01",
          date_kind: "day",
          genres: ["Fantasy"],
          score: 95,
          explanation: ["Strong match"],
          status: "recommended",
          source_name: "Feed A",
        },
      ],
      decisions: [],
      decisions_has_more: false,
      history: [],
      sources: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "",
        librarr_api_key_set: false,
        librarr_media_type: "audiobook",
        digest: {},
      },
    };
    const digestReviewPayload = {
      period: "2026-W39",
      ids: [1, 30],
      found: true,
      items: [
        overview.recommendations[0],
        {
          id: 30,
          title: "Rank Thirty Digest Gem",
          author: "Writer Thirty",
          description: "Ranked below page one",
          cover_url: "https://covers.example/30.jpg",
          source_url: "https://books.example/30",
          release_date: "2026-09-15",
          date_kind: "day",
          genres: ["Xenolinguistics"],
          score: 83,
          explanation: ["Digest standout"],
          status: "recommended",
          source_name: "Feed B",
        },
      ],
    };
    const fetchMock = vi.fn().mockImplementation((input: string | URL) => {
      const url = String(input);
      if (url.includes("/api/digest/review")) {
        return new Response(JSON.stringify(digestReviewPayload), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(overview), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = (await load({
      url: new URL("http://afterword.test/?view=discover&digest=1&digest_period=2026-W39"),
      locals: { user: null },
    } as never)) as {
      recommendations_has_more: boolean;
      recommendation_genres: string[];
      decision_genres: string[];
      digestReview: {
        requested: boolean;
        stale: boolean;
        period: string | null;
        ids: number[];
        books: Array<{ id: number; title: string; recommendation_run_id?: string | null }>;
      };
    };

    expect(result.recommendations_has_more).toBe(true);
    expect(result.recommendation_genres).toEqual(["Fantasy", "Xenolinguistics"]);
    expect(result.decision_genres).toEqual(["Historical Fiction", "Solarpunk"]);
    expect(result.digestReview.requested).toBe(true);
    expect(result.digestReview.ids).toEqual([1, 30]);
    expect(result.digestReview.books).toEqual([
      expect.objectContaining({
        id: 1,
        title: "Top Ranked Pick",
        recommendation_run_id: "run-9999",
      }),
      expect.objectContaining({
        id: 30,
        title: "Rank Thirty Digest Gem",
        recommendation_run_id: "",
      }),
    ]);
  });

  it("marks unknown digest periods unrequested and returns an empty digestReview.books slice", async () => {
    const overview = {
      recommendations: [],
      history: [],
      sources: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "",
        librarr_api_key_set: false,
        librarr_media_type: "audiobook",
        digest: {},
      },
    };
    const fetchMock = vi.fn().mockImplementation((input: string | URL) => {
      if (String(input).includes("/api/digest/review")) {
        return new Response(
          JSON.stringify({ period: "2026-W52", ids: [], found: false }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify(overview), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = (await load({
      url: new URL("http://afterword.test/?view=discover&digest=1&digest_period=2026-W52"),
      locals: { user: null },
    } as never)) as {
      digestReview: {
        requested: boolean;
        ids: number[];
        books: unknown[];
      };
    };

    expect(result.digestReview).toEqual({
      requested: false,
      ids: [],
      books: [],
    });
  });

  it("exposes database storage and evidence quota metrics and compacts storage online", async () => {
    const overview = {
      recommendations: [],
      history: [],
      sources: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "",
        librarr_api_key_set: false,
        librarr_media_type: "audiobook",
        digest: {},
      },
    };
    const backupStatus = {
      enabled: true,
      interval_hours: 24,
      retention_count: 7,
      last_success_at: "2026-11-21T00:00:00+00:00",
      last_error: null,
      next_backup_at: "2026-11-22T00:00:00+00:00",
      backups: [],
      live_size_bytes: 12 * 1024 * 1024,
      reclaimable_bytes: 2 * 1024 * 1024,
      auto_vacuum_mode: "incremental",
      evidence_used_bytes: 64 * 1024 * 1024,
      evidence_quota_bytes: 512 * 1024 * 1024,
      evidence_status: "healthy",
    };
    let compactCalls = 0;
    const fetchMock = vi.fn().mockImplementation((input: string) => {
      if (String(input).includes("/api/backups/compact")) {
        compactCalls += 1;
        return Promise.resolve(
          new Response(
            JSON.stringify(
              compactCalls === 1
                ? {
                    reclaimed_bytes: 2 * 1024 * 1024,
                    after_bytes: 10 * 1024 * 1024,
                  }
                : {
                    reclaimed_bytes: 0,
                    after_bytes: 10 * 1024 * 1024,
                  },
            ),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      if (String(input).includes("/api/backups/evidence-rotation")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ evidence_rotation_enabled: true, status: backupStatus }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      if (String(input).includes("/api/backups/retention")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ retention_enabled: false, status: backupStatus }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      if (String(input).includes("/api/backups")) {
        return Promise.resolve(
          new Response(JSON.stringify(backupStatus), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify(overview), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const loaded = (await load({
      url: new URL("http://afterword.test/?view=settings"),
      locals: { user: null },
    } as never)) as {
      backupStatus: typeof backupStatus & {
        retention_enabled?: boolean;
        evidence_rotation_enabled?: boolean;
      };
    };
    expect(loaded.backupStatus.retention_enabled).toBe(false);
    expect(loaded.backupStatus.evidence_rotation_enabled).toBe(false);
    expect(loaded.backupStatus.live_size_bytes).toBe(12 * 1024 * 1024);
    expect(loaded.backupStatus.evidence_used_bytes).toBe(64 * 1024 * 1024);
    expect(loaded.backupStatus.evidence_status).toBe("healthy");

    const compactResult = await actions.compactStorage!({} as never);
    expect(compactResult).toEqual({
      message: "Storage compacted; reclaimed 2.0 MB. Live database: 10.0 MB.",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/backups/compact",
      expect.objectContaining({ method: "POST" }),
    );

    const alreadyCompact = await actions.compactStorage!({} as never);
    expect(alreadyCompact).toEqual({
      message: "Database already compact. Live database: 10.0 MB.",
    });

    const pauseForm = new FormData();
    pauseForm.set("enabled", "0");
    const paused = await actions.setStorageRetention!({
      request: new Request("http://afterword.test", { method: "POST", body: pauseForm }),
    } as never);
    expect(paused).toEqual({ message: "Automatic storage retention paused." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/backups/retention",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ enabled: false }),
      }),
    );

    const resumeForm = new FormData();
    resumeForm.set("enabled", "1");
    const resumed = await actions.setStorageRetention!({
      request: new Request("http://afterword.test", { method: "POST", body: resumeForm }),
    } as never);
    expect(resumed).toEqual({ message: "Automatic storage retention enabled." });

    const rotationForm = new FormData();
    rotationForm.set("enabled", "1");
    const rotationEnabled = await actions.setEvidenceRotation!({
      request: new Request("http://afterword.test", { method: "POST", body: rotationForm }),
    } as never);
    expect(rotationEnabled).toEqual({ message: "Replay evidence rotation enabled." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/backups/evidence-rotation",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ enabled: true }),
      }),
    );
  });

  it("removes a shortlisted or imported book with restore and exposes its undo token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 12, status: "recommended", decision_id: 77 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "12");
    body.set("status", "recommended");
    body.set("run_id", "run-shortlist-1");
    const result = await actions.decide!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({
      message: "Returned to Discover.",
      undo_id: 77,
      id: 12,
      undo_label: "restoring this book",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      action: "restore",
      run_id: "run-shortlist-1",
    });
  });

  it("forwards run_id when importing a shortlisted book to the Librarr wishlist", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 12, status: "imported", remote_id: "remote-12" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "12");
    body.set("run_id", "run-shortlist-1");
    const result = await actions.importLibrar!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);

    expect(result).toEqual({ message: "Book added to the Librarr wishlist." });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/recommendations/12/import",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ run_id: "run-shortlist-1" }),
      }),
    );
  });

  it("forwards a shared run_id and returns batch undo tokens for shortlistBulk", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          updated: 2,
          decisions: [
            { id: 7, decision_id: 21 },
            { id: 8, decision_id: 22 },
          ],
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("run_id", "run-digest-1");
    body.append("ids", "7");
    body.append("ids", "8");
    const result = await actions.shortlistBulk!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message: "2 books added to your shortlist.",
      undo_decisions: [
        { id: 7, decision_id: 21 },
        { id: 8, decision_id: 22 },
      ],
      undo_label: "shortlisting 2 books",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      ids: [7, 8],
      action: "save",
      run_id: "run-digest-1",
    });
  });

  it("loads active catalog sweep progress, remaining rows, and retry status in Settings", async () => {
    const overview = {
      recommendations: [],
      history: [],
      sources: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "",
        librarr_api_key_set: false,
        librarr_media_type: "audiobook",
        digest: {},
      },
    };
    const quality = {
      statuses: [
        { quality_status: "accepted", count: 80 },
        { quality_status: "quarantine", count: 12 },
      ],
      identifiers: { total: 92, with_isbn13: 70, with_catalog_match: 80 },
      flags: { catalog_unmatched: 12 },
      version: "candidate-quality-v4",
      audit: {
        status: "failed",
        active: false,
        mode: "full",
        last_mode: "full",
        batch_size: 50,
        subbatch_size: 10,
        remaining: 42,
        progress: 0.6,
        started_at: "2026-11-21 20:00:00.000000",
        completed_at: null,
        job_id: "job-audit-1",
        last_error: "Catalog provider timed out",
        can_retry: true,
      },
    };
    const fetchMock = vi.fn().mockImplementation((input: string) => {
      const payload = input.includes("/api/candidates/quality") ? quality : overview;
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = (await load({
      url: new URL("http://afterword.test/?view=settings"),
      locals: { user: null },
    } as never)) as {
      catalogQuality: {
        audit: {
          status: string;
          mode: string;
          remaining: number;
          progress: number;
          last_error: string | null;
          can_retry: boolean;
        };
      };
    };
    expect(result.catalogQuality.audit).toMatchObject({
      status: "failed",
      mode: "full",
      remaining: 42,
      progress: 0.6,
      last_error: "Catalog provider timed out",
      can_retry: true,
    });
  });

  it("queues incremental, full, and retried catalog audits through the engine", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ job_id: "12345678-audit", mode: "full", remaining: 120 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ status: "running", error: null, result: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ status: "running", error: null, result: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ job_id: "87654321-retry", mode: "full", remaining: 70 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "complete",
            error: null,
            result: JSON.stringify({ remaining: 20 }),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const fullForm = new FormData();
    fullForm.set("mode", "full");
    const fullPending = actions.auditCatalog!({
      request: new Request("http://afterword.test", {
        method: "POST",
        body: fullForm,
      }),
    } as never);
    await vi.advanceTimersByTimeAsync(2 * 250);
    await expect(fullPending).resolves.toEqual({
      message: "Full catalog audit queued in bounded 50-book batches (120 books remaining).",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/candidates/audit",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      mode: "full",
    });

    const retryPending = actions.retryCatalogAudit!({} as never);
    await vi.advanceTimersByTimeAsync(250);
    await expect(retryPending).resolves.toEqual({
      message:
        "Catalog audit retry batch finished; 20 books remaining in the background.",
    });
    expect(JSON.parse(String((fetchMock.mock.calls[3][1] as RequestInit).body))).toEqual({
      retry: true,
    });
  });

  it("surfaces skipped reasons and delivered book counts from manual digest runs", async () => {
    vi.useFakeTimers();
    try {
      const outcomes = [
        {
          payload: { status: "skipped", reason: "no_new_recommendations", items: 0 },
          expected: "Digest skipped: no new recommendations matched your minimum score.",
        },
        {
          payload: { status: "skipped", reason: "already_delivered", period: "2026-W47", items: 4 },
          expected: "Digest skipped: 2026-W47 was already delivered.",
        },
        {
          payload: { status: "skipped", reason: "no_channels", items: 4 },
          expected: "Digest skipped: enable and save at least one delivery channel first.",
        },
        {
          payload: { status: "sent", period: "2026-W47", items: 3 },
          expected: "Digest sent with 3 books.",
        },
      ];

      for (const { payload, expected } of outcomes) {
        const fetchMock = vi
          .fn()
          .mockResolvedValueOnce(
            new Response(JSON.stringify({ job_id: "job-digest-1" }), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          )
          .mockResolvedValueOnce(
            new Response(
              JSON.stringify({
                status: "complete",
                error: null,
                result: JSON.stringify(payload),
              }),
              { status: 200, headers: { "content-type": "application/json" } },
            ),
          );
        vi.stubGlobal("fetch", fetchMock);
        const promise = actions.runDigest!({} as never);
        await vi.advanceTimersByTimeAsync(500);
        await expect(promise).resolves.toEqual({ message: expected });
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces top-level retry_delivery error strings on failed digest retries", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ job_id: "job-retry-1" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              status: "complete",
              error: null,
              result: JSON.stringify({
                id: "550e8400-e29b-41d4-a716-446655440000",
                status: "failed",
                error: "Discord webhook returned HTTP 404",
              }),
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      vi.stubGlobal("fetch", fetchMock);
      const body = new FormData();
      body.set("id", "550e8400-e29b-41d4-a716-446655440000");
      const promise = actions.retryDigest!({
        request: new Request("http://afterword.test", { method: "POST", body }),
      } as never);
      await vi.advanceTimersByTimeAsync(500);
      const result = (await promise) as { status: number; data: { message: string; error: boolean } };
      expect(result.status).toBe(502);
      expect(result.data.message).toBe("Discord webhook returned HTTP 404");
    } finally {
      vi.useRealTimers();
    }
  });

  it("imports a StoryGraph CSV export through the unified CSV action and names The StoryGraph", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ imported: 3, source: "storygraph_csv", job_id: "job-sg" }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set(
      "file",
      new File(
        ["Title,Authors,Read Status,Star Rating\nPiranesi,Susanna Clarke,read,4.75\n"],
        "storygraph.csv",
        { type: "text/csv" },
      ),
    );
    const result = await actions.importCsv!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message: "Imported 3 books from The StoryGraph.",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/import/csv",
    );
  });

  it("retains importGoodreadsCsv compatibility and surfaces 400 format errors clearly", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ imported: 1, source: "goodreads_csv", job_id: "job-gr" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "CSV must match Goodreads or The StoryGraph export format",
          }),
          {
            status: 400,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const validBody = new FormData();
    validBody.set(
      "file",
      new File(["Title,Author,My Rating\nDune,Frank Herbert,5\n"], "goodreads.csv", {
        type: "text/csv",
      }),
    );
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: validBody }),
      } as never),
    ).resolves.toEqual({
      message: "Imported 1 book from Goodreads.",
    });

    const invalidBody = new FormData();
    invalidBody.set(
      "file",
      new File(["Heading,Amount\nSomething,10\n"], "budget.csv", {
        type: "text/csv",
      }),
    );
    const failed = (await actions.importCsv!({
      request: new Request("http://afterword.test", { method: "POST", body: invalidBody }),
    } as never)) as { status: number; data: { message: string } };
    expect(failed.status).toBe(400);
    expect(failed.data.message).toContain("Goodreads or The StoryGraph");

    const emptyResult = (await actions.importCsv!({
      request: new Request("http://afterword.test", { method: "POST", body: new FormData() }),
    } as never)) as { status: number; data: { message: string } };
    expect(emptyResult.status).toBe(400);
    expect(emptyResult.data.message).toBe(
      "Choose a Goodreads or StoryGraph CSV export.",
    );
  });

  it("validates CSV size, catches 413 body errors, and preserves engine error statuses", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const emptyForm = new FormData();
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: emptyForm }),
      } as never),
    ).resolves.toMatchObject({
      status: 400,
      data: { message: "Choose a Goodreads or StoryGraph CSV export.", error: true },
    });

    const oversizedForm = new FormData();
    oversizedForm.set(
      "file",
      new File([new Uint8Array(10_000_001)], "goodreads.csv", { type: "text/csv" }),
    );
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: oversizedForm }),
      } as never),
    ).resolves.toMatchObject({
      status: 413,
      data: { message: expect.stringContaining("10 MB"), error: true },
    });

    const oversizedStoryGraphForm = new FormData();
    oversizedStoryGraphForm.set(
      "file",
      new File([new Uint8Array(10_000_001)], "storygraph.csv", { type: "text/csv" }),
    );
    await expect(
      actions.importCsv!({
        request: new Request("http://afterword.test", {
          method: "POST",
          body: oversizedStoryGraphForm,
        }),
      } as never),
    ).resolves.toMatchObject({
      status: 413,
      data: { message: expect.stringContaining("10 MB"), error: true },
    });

    const streamErrorRequest = new Request("http://afterword.test", {
      method: "POST",
      // @ts-expect-error Node fetch requires half-duplex for streaming request bodies
      duplex: "half",
      headers: {
        "x-bookward-profile": "alice-profile",
        "content-type": "multipart/form-data; boundary=----bookward",
      },
      body: new ReadableStream({
        start(controller) {
          controller.error(Object.assign(new Error("Payload Too Large"), { status: 413 }));
        },
      }),
    });
    await expect(
      actions.importGoodreadsCsv!({ request: streamErrorRequest } as never),
    ).resolves.toMatchObject({
      status: 413,
      data: { message: expect.stringContaining("10 MB"), error: true },
    });
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ detail: "CSV must match Goodreads or The StoryGraph export format" }),
        {
          status: 400,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    const invalidCsvForm = new FormData();
    invalidCsvForm.set("file", new File(["bad,csv\n"], "bad.csv", { type: "text/csv" }));
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: invalidCsvForm }),
      } as never),
    ).resolves.toMatchObject({
      status: 400,
      data: {
        message: "CSV must match Goodreads or The StoryGraph export format",
        error: true,
      },
    });

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "CSV is larger than 10 MB" }), {
        status: 413,
        headers: { "content-type": "application/json" },
      }),
    );
    const engine413Form = new FormData();
    engine413Form.set("file", new File(["Title,Author\nDune,Herbert\n"], "goodreads.csv", { type: "text/csv" }));
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: engine413Form }),
      } as never),
    ).resolves.toMatchObject({
      status: 413,
      data: { message: "CSV is larger than 10 MB", error: true },
    });

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ imported: 3, source: "goodreads_csv" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const validForm = new FormData();
    validForm.set("file", new File(["Title,Author\nDune,Frank Herbert\n"], "goodreads.csv", { type: "text/csv" }));
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: validForm }),
      } as never),
    ).resolves.toEqual({ message: "Imported 3 books from Goodreads." });
  });

  it("forwards missing initial Librarr API key validation from the engine without pre-fetching overview", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(
        JSON.stringify({ detail: "Enter a Librarr API key for the first connection." }),
        {
          status: 400,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("url", "http://librarr:5050");
    body.set("apiKey", "");
    body.set("mediaType", "audiobook");
    const result = (await actions.configureLibrar!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never)) as { status: number; data: { message: string; error: true } };
    expect(result.status).toBe(400);
    expect(result.data).toEqual({
      message: "Enter a Librarr API key for the first connection.",
      error: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/settings");
  });

  it("saves embedding settings in a single partial PUT request without pre-fetching overview", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ saved: true, embedding: { ok: true, checked: true } }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("backend", "ollama");
    body.set("model", "qwen3-embedding:0.6b");
    body.set("url", "http://ollama:11434");
    body.set("apiKey", "embed-secret");
    const result = await actions.configureEmbeddings!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "Embedding provider saved and checked." });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/settings");
    expect(fetchMock.mock.calls[0][0]).not.toContain("/api/overview");
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      embedding_backend: "ollama",
      embedding_model: "qwen3-embedding:0.6b",
      embedding_url: "http://ollama:11434",
      embedding_api_key: "embed-secret",
    });
  });

  it("toggles the default source in one engine request and forwards 404 when absent", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 1, enabled: 0, job_id: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Default source not found." }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const toggled = await actions.toggleDefault!({
      request: new Request("http://afterword.test", { method: "POST" }),
    } as never);
    expect(toggled).toEqual({ message: "Upcoming-book source updated." });

    const missing = (await actions.toggleDefault!({
      request: new Request("http://afterword.test", { method: "POST" }),
    } as never)) as { status: number; data: { message: string; error: true } };
    expect(missing.status).toBe(404);
    expect(missing.data).toEqual({
      message: "Default source not found.",
      error: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/sources/default/toggle",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/sources/default/toggle",
    );
  });
});
