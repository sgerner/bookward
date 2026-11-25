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
        "Bookward's recommendation engine is unavailable. connect ECONNREFUSED 127.0.0.1:8000",
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
      data: { message: "Connection refused", error: true },
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
      data: { message: "Sync unreachable", error: true },
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

  it("attaches recommendation_run_id only to recommended discovery books on overview load", async () => {
    const overview = {
      recommendation_run_id: "run-discover-only",
      recommendations_has_more: false,
      recommendations: [
        {
          id: 1,
          title: "Discovery Pick",
          author: "Author A",
          description: "",
          cover_url: "",
          source_url: "",
          release_date: null,
          date_kind: "unknown",
          genres: [],
          score: 92,
          metadata_confidence: 1,
          explanation: ["Strong match"],
          status: "recommended",
          source_name: "Demo",
        },
        {
          id: 2,
          title: "Shortlisted Pick",
          author: "Author B",
          description: "",
          cover_url: "",
          source_url: "",
          release_date: null,
          date_kind: "unknown",
          genres: [],
          score: 88,
          metadata_confidence: 1,
          explanation: ["Saved earlier"],
          status: "saved",
          source_name: "Demo",
        },
      ],
      decisions: [],
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
        source_sync_interval_hours: 24,
        digest: {},
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        () =>
          new Response(JSON.stringify(overview), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    const result = (await load({
      url: new URL("http://afterword.test/"),
      locals: { user: null },
    } as never)) as {
      recommendations_has_more: boolean;
      books: Array<{ id: number; status: string; recommendation_run_id: string }>;
    };
    expect(result.recommendations_has_more).toBe(false);
    expect(result.books).toEqual([
      expect.objectContaining({
        id: 1,
        status: "recommended",
        recommendation_run_id: "run-discover-only",
      }),
      expect.objectContaining({
        id: 2,
        status: "saved",
        recommendation_run_id: "",
      }),
    ]);
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

  it("disables impression tracking when loading Settings or submitting Settings form actions", async () => {
    const overview = {
      recommendation_run_id: "",
      recommendations: [],
      decisions: [],
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
        source_sync_interval_hours: 24,
        digest: {},
      },
    };
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Response(JSON.stringify(overview), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await load({
      url: new URL("http://afterword.test/?view=settings"),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // Invalidated data reload with explicit ?view=discover tracks impressions; ?view=saved does not.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/?view=discover"),
      request: new Request(
        "http://afterword.test/__data.json?view=discover&x-sveltekit-invalidated=11",
      ),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).not.toContain("track_impressions=false");

    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/?view=saved"),
      request: new Request(
        "http://afterword.test/__data.json?view=saved&x-sveltekit-invalidated=11",
      ),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // Non-JS POST form submissions read explicit view from FormData (discover vs settings).
    fetchMock.mockClear();
    const settingsForm = new FormData();
    settingsForm.set("view", "settings");
    await load({
      url: new URL("http://afterword.test/?/configureEmbeddings"),
      request: new Request("http://afterword.test/?/configureEmbeddings", {
        method: "POST",
        body: settingsForm,
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    fetchMock.mockClear();
    const discoverForm = new FormData();
    discoverForm.set("view", "discover");
    await load({
      url: new URL("http://afterword.test/?/decide"),
      request: new Request("http://afterword.test/?/decide", {
        method: "POST",
        body: discoverForm,
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).not.toContain("track_impressions=false");
  });

  it("fails closed on POST and x-sveltekit-invalidated loads without an explicit view", async () => {
    const overview = {
      recommendation_run_id: "",
      recommendations: [],
      decisions: [],
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
        source_sync_interval_hours: 24,
        digest: {},
      },
    };
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Response(JSON.stringify(overview), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    // 1. POST form submission lacking `view` in both query string and FormData fails closed
    // even when Referer points to Discover.
    const formWithoutView = new FormData();
    formWithoutView.set("id", "42");
    formWithoutView.set("status", "saved");
    await load({
      url: new URL("http://afterword.test/?/decide"),
      request: new Request("http://afterword.test/?/decide", {
        method: "POST",
        body: formWithoutView,
        headers: { referer: "http://afterword.test/?view=discover" },
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 2. POST without a form body and without `view` fails closed.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/?/configureEmbeddings"),
      request: new Request("http://afterword.test/?/configureEmbeddings", {
        method: "POST",
        headers: { referer: "http://afterword.test/" },
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 3. POST with an unrecognized `view` value fails closed on mutation re-renders.
    fetchMock.mockClear();
    const unknownViewForm = new FormData();
    unknownViewForm.set("view", "unexpected-tab");
    await load({
      url: new URL("http://afterword.test/?/decide"),
      request: new Request("http://afterword.test/?/decide", {
        method: "POST",
        body: unknownViewForm,
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 4. x-sveltekit-invalidated on request.url (__data.json) without explicit ?view= fails closed.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/"),
      request: new Request(
        "http://afterword.test/__data.json?x-sveltekit-trailing-slash=1&x-sveltekit-invalidated=11",
        { headers: { referer: "http://afterword.test/" } },
      ),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 5. x-sveltekit-invalidated directly on load `url` without explicit ?view= fails closed.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/?x-sveltekit-invalidated=11"),
      request: new Request("http://afterword.test/?x-sveltekit-invalidated=11"),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 6. x-sveltekit-invalidated header without explicit ?view= fails closed.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/"),
      request: new Request("http://afterword.test/__data.json", {
        headers: { "x-sveltekit-invalidated": "11", referer: "http://afterword.test/" },
      }),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).toContain("track_impressions=false");

    // 7. Standard initial GET / without x-sveltekit-invalidated still tracks Discover impressions.
    fetchMock.mockClear();
    await load({
      url: new URL("http://afterword.test/"),
      request: new Request("http://afterword.test/"),
      locals: { user: null },
    } as never);
    expect(fetchMock.mock.calls[0][0]).not.toContain("track_impressions=false");
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

  it("omits empty run_id on shortlist and history decide actions so engine fallback attribution applies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 17, status: "rejected", decision_id: 99 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const body = new FormData();
    body.set("id", "17");
    body.set("status", "rejected");
    body.set("run_id", "");

    const result = await actions.decide!({
      request: new Request("http://afterword.test/?/decide&view=saved", {
        method: "POST",
        body,
      }),
    } as never);

    expect(result).toMatchObject({
      message: "Passed on this recommendation.",
      undo_id: 99,
      id: 17,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/recommendations/17/feedback"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ action: "reject" }),
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
