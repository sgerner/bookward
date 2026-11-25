const env = process.env;
import type { RequestEvent } from "@sveltejs/kit";
import { sanitizeEngineMessage } from "#lib/server/engine.js";

const engineBase = (env.ENGINE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const forwardedHeaders = [
  "accept",
  "authorization",
  "content-type",
  "idempotency-key",
  "x-api-key",
];
const nestedApiMethods = "GET, POST, PUT, DELETE, OPTIONS";
const rootApiMethods = "GET, OPTIONS";

function corsHeaders(request: Request, methods = nestedApiMethods) {
  const origin = request.headers.get("origin");
  const headers = new Headers({
    "access-control-allow-headers": "Accept, Authorization, Content-Type, Idempotency-Key, X-API-Key",
    "access-control-allow-methods": methods,
    "access-control-max-age": "86400",
  });
  headers.set("access-control-allow-origin", origin || "*");
  if (origin) headers.set("vary", "Origin");
  return headers;
}

function encodedApiPath(path: string | undefined) {
  if (!path) return '';
  // SvelteKit has already decoded route params. Decoding a second time would
  // turn literal percent escapes in a segment into path separators or dots.
  const decoded = path.replaceAll('\\', '/');
  const segments = decoded.split('/');
  if (segments.some((segment) => segment === '.' || segment === '..')) return null;
  return segments.map((segment) => encodeURIComponent(segment)).join('/');
}

export async function proxyApi(event: RequestEvent) {
  const encodedPath = encodedApiPath(event.params.path);
  if (encodedPath === null) {
    return new Response(JSON.stringify({ detail: "Invalid API path" }), {
      status: 404,
      headers: new Headers({
        ...Object.fromEntries(corsHeaders(event.request)),
        "content-type": "application/json",
        "cache-control": "no-store",
      }),
    });
  }
  const methods = encodedPath ? nestedApiMethods : rootApiMethods;
  const path = encodedPath ? `/${encodedPath}` : "";
  const target = `${engineBase}/api/v1${path}${event.url.search}`;

  if (event.request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders(event.request, methods) });
  }

  const headers = new Headers();
  for (const name of forwardedHeaders) {
    const value = event.request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (env.ENGINE_SERVICE_SECRET) headers.set("x-bookward-service", env.ENGINE_SERVICE_SECRET);

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

  const responseHeaders = corsHeaders(event.request, methods);
  if (!upstream.ok) {
    for (const name of ["etag", "location", "retry-after", "www-authenticate"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    responseHeaders.set("content-type", "application/json");
    responseHeaders.set("cache-control", "no-store");
    if (event.request.method === "HEAD") {
      await upstream.body?.cancel().catch(() => {});
      return new Response(null, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    }
    const rawBody = await upstream.text().catch(() => "");
    let rawDetail: unknown = rawBody || undefined;
    if (rawBody.trim()) {
      try {
        const parsed = JSON.parse(rawBody) as unknown;
        rawDetail =
          parsed && typeof parsed === "object" && "detail" in parsed
            ? (parsed as { detail?: unknown }).detail
            : parsed;
      } catch {
        rawDetail = rawBody;
      }
    }
    const sanitizedDetail =
      upstream.status < 500 &&
      Array.isArray(rawDetail) &&
      rawDetail.every((item) => Boolean(item && typeof item === "object"))
        ? rawDetail.map((item) => {
            const entry = item as { type?: unknown; loc?: unknown; msg?: unknown };
            return {
              ...(typeof entry.type === "string" ? { type: entry.type } : {}),
              ...(Array.isArray(entry.loc)
                ? {
                    loc: entry.loc.filter(
                      (part): part is string | number =>
                        typeof part === "string" || typeof part === "number",
                    ),
                  }
                : {}),
              msg: sanitizeEngineMessage(entry.msg, upstream.status),
            };
          })
        : sanitizeEngineMessage(rawDetail, upstream.status);
    return new Response(JSON.stringify({ detail: sanitizedDetail }), {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  }
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
