import { afterEach, describe, expect, it, vi } from "vitest";
import { actions, load } from "./+page.server";

afterEach(() => {
  vi.useRealTimers();
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
    const overview = {
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

    expect(result).toEqual({ message: "Digest delivery retry queued." });
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

  it("validates Goodreads CSV size, catches 413 body errors, and preserves engine error statuses", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const emptyForm = new FormData();
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: emptyForm }),
      } as never),
    ).resolves.toMatchObject({
      status: 400,
      data: { message: "Choose a Goodreads CSV export.", error: true },
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
      new Response(JSON.stringify({ detail: "This does not look like a Goodreads export" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
    );
    const invalidCsvForm = new FormData();
    invalidCsvForm.set("file", new File(["bad,csv\n"], "bad.csv", { type: "text/csv" }));
    await expect(
      actions.importGoodreadsCsv!({
        request: new Request("http://afterword.test", { method: "POST", body: invalidCsvForm }),
      } as never),
    ).resolves.toMatchObject({
      status: 400,
      data: { message: "This does not look like a Goodreads export", error: true },
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
      new Response(JSON.stringify({ imported: 3 }), {
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
    ).resolves.toEqual({ message: "Imported 3 Goodreads books." });
  });
});
