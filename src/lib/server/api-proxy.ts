import { env } from "$env/dynamic/private";
import type { RequestEvent } from "@sveltejs/kit";

const engineBase = (env.ENGINE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const forwardedHeaders = [
  "accept",
  "authorization",
  "content-type",
  "idempotency-key",
  "x-api-key",
];

function corsHeaders(request: Request) {
  const origin = request.headers.get("origin");
  const headers = new Headers({
    "access-control-allow-headers": "Accept, Authorization, Content-Type, Idempotency-Key, X-API-Key",
    "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
    "access-control-max-age": "86400",
  });
  headers.set("access-control-allow-origin", origin || "*");
  if (origin) headers.set("vary", "Origin");
  return headers;
}

export async function proxyApi(event: RequestEvent) {
  const path = event.params.path ? `/${event.params.path}` : "";
  const target = `${engineBase}/api/v1${path}${event.url.search}`;

  if (event.request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders(event.request) });
  }

  const headers = new Headers();
  for (const name of forwardedHeaders) {
    const value = event.request.headers.get(name);
    if (value) headers.set(name, value);
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: event.request.method,
      headers,
      body: ["GET", "HEAD"].includes(event.request.method)
        ? undefined
        : await event.request.arrayBuffer(),
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Bookward API is unavailable" }), {
      status: 503,
      headers: new Headers({
        ...Object.fromEntries(corsHeaders(event.request)),
        "content-type": "application/json",
        "cache-control": "no-store",
      }),
    });
  }

  const responseHeaders = corsHeaders(event.request);
  for (const name of ["cache-control", "content-type", "etag", "location", "www-authenticate"]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
