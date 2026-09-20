import { afterEach, describe, expect, it, vi } from "vitest";
import { proxyApi } from "./api-proxy";

afterEach(() => vi.unstubAllGlobals());

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
});
