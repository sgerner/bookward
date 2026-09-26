import { afterEach, describe, expect, it, vi } from "vitest";
import { actions, load } from "./+page.server";

afterEach(() => vi.unstubAllGlobals());

describe("page actions", () => {
  it("keeps the Settings view compatible with an engine before the token migration", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
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
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = (await load({
      url: new URL("http://afterword.test/?view=settings"),
    } as never)) as { profile: { api_tokens: unknown[] } };
    expect(result.profile.api_tokens).toEqual([]);
  });

  it("loads recommendations without creating or forwarding a device cookie", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          recommendation_run_id: "run-1234",
          recommendations: [],
          decisions: [
            {
              id: 9,
              title: "Deferred book",
              author: "A Writer",
              description: "A description",
              cover_url: "https://covers.example/deferred.jpg",
              source_url: "https://books.example/deferred",
              release_date: null,
              date_kind: "unknown",
              genres: ["Fantasy"],
              score: 81,
              metadata_confidence: 0.8,
              explanation: ["A good match"],
              status: "maybe_later",
              source_name: "A source",
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
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = (await load({
      url: new URL("http://afterword.test/"),
    } as never)) as {
      recommendation_run_id: string;
      decisions: Array<{ id: number; status: string; reason: string }>;
    };
    expect(result.recommendation_run_id).toBe("run-1234");
    expect(result.decisions).toEqual([
      expect.objectContaining({
        id: 9,
        status: "maybe_later",
        reason: "A good match",
      }),
    ]);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/overview?recommendation_limit=24");
    expect(fetchMock.mock.calls[0][1]?.headers).not.toHaveProperty("x-bookward-session");
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
      message: "Added to your read list with a 5-star rating.",
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

  it("saves shortlist reading transitions and optional completion ratings", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "finished" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const finished = new FormData();
    finished.set("id", "42");
    finished.set("status", "finished");
    finished.set("rating", "5");
    await actions.readingProgress!({
      request: new Request("http://afterword.test", { method: "POST", body: finished }),
    } as never);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/reading-list/42",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ status: "finished", rating: 5 }),
      }),
    );

    const pinned = new FormData();
    pinned.set("id", "42");
    pinned.set("up_next", "true");
    await actions.readingProgress!({
      request: new Request("http://afterword.test", { method: "POST", body: pinned }),
    } as never);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/reading-list/42",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ up_next: true }),
      }),
    );
  });

  it("rejects invalid shortlist transitions locally", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "42");
    body.set("status", "finished");
    body.set("rating", "6");
    const result = await actions.readingProgress!({
      request: new Request("http://afterword.test", { method: "POST", body }),
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

  it("saves normalized source genre filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ saved: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("id", "7");
    body.set("includeGenres", " Science fiction, fantasy, science fiction ");
    body.set("excludeGenres", " romance ");
    const result = await actions.configureSourceFilters!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({
      message: "Source filters updated; a fresh scan is queued.",
    });
    expect(fetchMock).toHaveBeenCalledWith(
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
    const overview = {
      sources: [],
      recommendations: [],
      history: [],
      settings: {
        embedding_backend: "local",
        embedding_model: "hashing-768",
        embedding_url: "",
        embedding_api_key_set: false,
        librarr_url: "http://librarr:5050",
        librarr_api_key_set: true,
        librarr_media_type: "audiobook",
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(overview), {
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
    body.set("url", "http://librarr:5050");
    body.set("apiKey", "");
    body.set("mediaType", "ebook");
    const result = await actions.configureLibrar!({
      request: new Request("http://afterword.test", { method: "POST", body }),
    } as never);
    expect(result).toEqual({ message: "Librarr connection saved for ebooks." });
    const request = fetchMock.mock.calls[1][1] as RequestInit;
    expect(JSON.parse(String(request.body))).toMatchObject({
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


});
