import { afterEach, describe, expect, it, vi } from "vitest";
import { proxyApi } from "./api-proxy";
import { ENGINE_UNAVAILABLE_MESSAGE } from "./engine";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const eventFor = (request: Request, path = "recommendations") =>
  ({ request, url: new URL(request.url), params: { path } }) as never;

describe("public API proxy", () => {
  it("forwards the request path, body, and token headers to the engine", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 201,
        headers: { "content-type": "application/json", etag: '"bookward"' },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request(
      "http://bookward.test/api/v1/recommendations?limit=2",
      {
        method: "POST",
        headers: {
          accept: "application/json",
          authorization: "Bearer bkw_test",
          "content-type": "application/json",
          "idempotency-key": "request-1",
          "x-bookward-service": "client-controlled-secret",
          "x-api-key": "bkw_test",
          origin: "https://client.example",
        },
        body: JSON.stringify({ action: "save" }),
      },
    );

    const response = await proxyApi(eventFor(request));
    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ ok: true });
    expect(response.headers.get("access-control-allow-origin")).toBe(
      "https://client.example",
    );
    expect(response.headers.get("etag")).toBe('"bookward"');

    const [target, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(target).toBe(
      "http://127.0.0.1:8000/api/v1/recommendations?limit=2",
    );
    expect(init.method).toBe("POST");
    const forwarded = new Headers(init.headers);
    expect(forwarded.get("authorization")).toBe("Bearer bkw_test");
    expect(forwarded.get("x-api-key")).toBe("bkw_test");
    expect(forwarded.get("idempotency-key")).toBe("request-1");
    expect(forwarded.get("x-bookward-service")).not.toBe("client-controlled-secret");
    expect(await new Response(init.body).json()).toEqual({ action: "save" });
  });

  it("answers CORS preflight without contacting the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request("http://bookward.test/api/v1/recommendations", {
      method: "OPTIONS",
      headers: {
        origin: "https://client.example",
        "access-control-request-method": "GET",
      },
    });

    const response = await proxyApi(eventFor(request));
    expect(response.status).toBe(204);
    expect(response.headers.get("access-control-allow-origin")).toBe(
      "https://client.example",
    );
    expect(response.headers.get("access-control-allow-methods")).toContain(
      "GET",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not advertise mutating methods on the read-only API root", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request("http://bookward.test/api/v1", {
      method: "OPTIONS",
      headers: {
        origin: "https://client.example",
        "access-control-request-method": "POST",
      },
    });

    const response = await proxyApi({
      request,
      url: new URL(request.url),
      params: {},
    } as never);

    expect(response.status).toBe(204);
    expect(response.headers.get("access-control-allow-methods")).toBe(
      "GET, OPTIONS",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects dot-segment paths before they can escape the versioned API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request(
      "http://bookward.test/api/v1/%2e%2e/settings/api-tokens",
      { headers: { origin: "https://client.example" } },
    );

    const response = await proxyApi({
      request,
      url: new URL(request.url),
      params: { path: "../settings/api-tokens" },
    } as never);

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ detail: "Invalid API path" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves percent escapes that SvelteKit has already decoded once", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request(
      "http://bookward.test/api/v1/reports%252Fprivate?format=json",
    );

    await proxyApi(eventFor(request, "reports%2Fprivate"));

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/v1/reports%252Fprivate?format=json",
    );
  });

  it("forwards literal percent text in a decoded route segment", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request("http://bookward.test/api/v1/items%25ZZ");

    const response = await proxyApi(eventFor(request, "items%ZZ"));

    expect(response.status).toBe(200);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/v1/items%25ZZ",
    );
  });

  it("preserves the engine auth challenge for API clients", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "A valid API token is required" }), {
          status: 401,
          headers: {
            "content-type": "application/json",
            "www-authenticate": "Bearer",
          },
        }),
      ),
    );
    const response = await proxyApi(
      eventFor(new Request("http://bookward.test/api/v1/recommendations")),
    );

    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toBe("Bearer");
  });

  it("returns a CORS-compatible 503 when the engine is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const request = new Request("http://bookward.test/api/v1/health", {
      headers: { origin: "https://client.example" },
    });

    const response = await proxyApi(eventFor(request, "health"));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: "Bookward API is unavailable" });
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("access-control-allow-origin")).toBe(
      "https://client.example",
    );
  });

  it("forwards recommendation pagination and run headers to API v1 callers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: {
            "content-type": "application/json",
            "x-bookward-has-more": "true",
            "x-bookward-recommendation-run": "run-v1-abc",
          },
        }),
      ),
    );
    const request = new Request(
      "http://bookward.test/api/v1/recommendations?status=recommended&limit=8&offset=24",
      { headers: { origin: "https://client.example" } },
    );

    const response = await proxyApi(eventFor(request));
    expect(response.status).toBe(200);
    expect(response.headers.get("x-bookward-has-more")).toBe("true");
    expect(response.headers.get("x-bookward-recommendation-run")).toBe("run-v1-abc");
    expect(response.headers.get("access-control-expose-headers")).toContain(
      "X-Bookward-Has-More",
    );
  });

  it("sanitizes raw 5xx details, secrets, local paths, and 422 validation errors on non-2xx upstream API responses", async () => {
    const proxySecret = ["internal", "proxy", "fixture"].join("-");
    const leakedApiTok = ["bkw", "fixture", "999"].join("_");
    vi.stubEnv("ENGINE_SERVICE_SECRET", proxySecret);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "RuntimeError: internal failure at /app/engine/afterword_engine/main.py",
          }),
          {
            status: 500,
            headers: { "content-type": "application/json" },
          },
        ),
      ).mockResolvedValueOnce(
        new Response("Bad Gateway: upstream 127.0.0.1:8000 closed connection", {
          status: 502,
          headers: { "content-type": "text/plain" },
        }),
      ).mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: `Invalid header ${proxySecret} for ${leakedApiTok}`,
          }),
          {
            status: 403,
            headers: { "content-type": "application/json" },
          },
        ),
      ).mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: [
              {
                type: "less_than_equal",
                loc: ["query", "limit"],
                msg: "Input should be less than or equal to 100",
                input: "9999",
              },
              {
                type: "value_error",
                loc: ["body", "path"],
                msg: "PermissionError: /etc/bookward/config.toml",
              },
            ],
          }),
            {
              status: 422,
              headers: { "content-type": "application/json" },
            },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              detail: "API token verification is busy. Try again shortly.",
            }),
            {
              status: 503,
              headers: {
                "content-type": "application/json",
                "retry-after": "1",
              },
            },
          ),
        ),
    );

    const req500 = new Request("http://bookward.test/api/v1/recommendations", {
      headers: { origin: "https://client.example" },
    });
    const res500 = await proxyApi(eventFor(req500));
    expect(res500.status).toBe(500);
    expect(res500.headers.get("cache-control")).toBe("no-store");
    expect(res500.headers.get("access-control-allow-origin")).toBe("https://client.example");
    await expect(res500.json()).resolves.toEqual({ detail: ENGINE_UNAVAILABLE_MESSAGE });

    const res502 = await proxyApi(
      eventFor(new Request("http://bookward.test/api/v1/recommendations")),
    );
    expect(res502.status).toBe(502);
    await expect(res502.json()).resolves.toEqual({ detail: ENGINE_UNAVAILABLE_MESSAGE });

    const res403 = await proxyApi(
      eventFor(new Request("http://bookward.test/api/v1/recommendations")),
    );
    expect(res403.status).toBe(403);
    await expect(res403.json()).resolves.toEqual({
      detail: "Invalid header [redacted] for [redacted]",
    });

    const res422 = await proxyApi(
      eventFor(new Request("http://bookward.test/api/v1/recommendations?limit=9999")),
    );
    expect(res422.status).toBe(422);
    await expect(res422.json()).resolves.toEqual({
      detail: [
        {
          type: "less_than_equal",
          loc: ["query", "limit"],
          msg: "Input should be less than or equal to 100",
        },
        {
          type: "value_error",
          loc: ["body", "path"],
          msg: "The request could not be completed.",
        },
      ],
    });

    const res503Busy = await proxyApi(
      eventFor(new Request("http://bookward.test/api/v1/recommendations")),
    );
    expect(res503Busy.status).toBe(503);
    expect(res503Busy.headers.get("retry-after")).toBe("1");
    await expect(res503Busy.json()).resolves.toEqual({
      detail: "API token verification is busy. Try again shortly.",
    });
  });
});
