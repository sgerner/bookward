const env = process.env;
import { currentSessionToken } from '#lib/server/request-context.js';

const base = (env.ENGINE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
function internalHeaders(headers?: HeadersInit, accept = 'application/json') {
  const merged = new Headers(headers);
  merged.set('accept', accept);
  if (env.ENGINE_SERVICE_SECRET) merged.set('x-bookward-service', env.ENGINE_SERVICE_SECRET);
  const session = currentSessionToken();
  if (session) merged.set('x-bookward-session', session);
  return merged;
}
export const ENGINE_UNAVAILABLE_MESSAGE = "Bookward's recommendation engine is unavailable.";

export class EngineError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly unavailable = status >= 500
  ) {
    super(message);
  }
}

export function isEngineUnavailable(error: unknown): boolean {
  return !(error instanceof EngineError) || error.unavailable || error.status >= 500;
}

const SAFE_EXACT_5XX_MESSAGES = new Set([
  'Profile database is unavailable because an interrupted restore has not been recovered. Restore a valid backup or ask an administrator to clear the restore journal.',
  'Your Bookward profile is temporarily unavailable. Please try again.',
  'The account was created but its profile could not be initialized',
  'The configured SSO issuer must use HTTPS',
  'The SSO callback URL must use HTTPS'
]);

const SAFE_INTEGRATION_502_PREFIX =
  /^(?:Librarr (?:request|search|download) failed:|Unable to fetch models\.dev catalog:|The SSO provider\b|Google Books preview failed:)/;

/**
 * Scrubs upstream error payloads before returning them to browsers or `/api/v1`
 * callers.
 *
 * - `status >= 500` fails closed to `ENGINE_UNAVAILABLE_MESSAGE` so raw 5xx
 *   exception strings, proxy gateway messages, or upstream hostnames never leak
 *   through; only explicit user-facing recovery/config messages and `502`
 *   third-party integration prefixes (Librarr, models.dev, OIDC discovery,
 *   Google Books) are preserved when free of internal leaks.
 * - `status < 500` preserves user-actionable validation/auth messages after
 *   redacting configured secrets, API/Bearer tokens, and URL credentials, while
 *   replacing stack traces, driver/OS exception names, and local filesystem
 *   paths with a generic message.
 */
export function sanitizeEngineMessage(detail: unknown, status: number): string {
  const fallback = status >= 500 ? ENGINE_UNAVAILABLE_MESSAGE : `Engine returned ${status}`;
  if (detail === undefined || detail === null) return fallback;
  if (typeof detail !== 'string') {
    return status >= 500 ? ENGINE_UNAVAILABLE_MESSAGE : 'The request could not be completed.';
  }
  if (!detail.trim()) return fallback;

  let cleaned = detail.trim();
  for (const rawSecret of [env.ENGINE_SERVICE_SECRET, env.AFTERWORD_SECRET_KEY]) {
    const secret = rawSecret?.trim();
    if (secret && cleaned.includes(secret)) {
      cleaned = cleaned.replaceAll(secret, '[redacted]');
    }
  }
  cleaned = cleaned
    .replace(/\bbkw_[A-Za-z0-9_-]+/g, '[redacted]')
    .replace(/\bBearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/(https?:\/\/)[^\s/@]+@/gi, '$1[redacted]@');

  const hasStackTrace =
    cleaned.includes('Traceback (most recent call last)') ||
    /\n\s*File "[^"]+", line \d+/.test(cleaned) ||
    /\n\s+at\s+\S+/.test(cleaned);
  const hasInternalException =
    /\b(?:sqlite3\.|OperationalError|IntegrityError|DatabaseError|PermissionError|FileNotFoundError|IsADirectoryError|KeyError|AttributeError)\b/i.test(cleaned) ||
    /\[Errno\s+-?\d+\]/i.test(cleaned);
  const hasLocalPath =
    /(?<![:\w])\/(?:app|data|etc|home|opt|proc|root|run|srv|tmp|usr|var|Users|Volumes)\/\S+/.test(cleaned) ||
    /(?<![:\w])\/\S*\.(?:db|sqlite|sqlite3|py|pyc|ts|js|key|pem|env|crt|log|sock|toml|lock)(?:-(?:wal|shm|journal))?\b/i.test(cleaned) ||
    /\b[A-Za-z]:\\[^\s"']+/i.test(cleaned);
  const hasInternalLeak = hasStackTrace || hasInternalException || hasLocalPath;

  if (status >= 500) {
    if (!hasInternalLeak && SAFE_EXACT_5XX_MESSAGES.has(cleaned)) {
      return cleaned;
    }
    if (status === 502 && !hasInternalLeak && SAFE_INTEGRATION_502_PREFIX.test(cleaned)) {
      return cleaned;
    }
    return ENGINE_UNAVAILABLE_MESSAGE;
  }
  if (hasInternalLeak) {
    return 'The request could not be completed.';
  }
  return cleaned;
}

export const GOODREADS_CSV_MAX_BYTES = 10_000_000;
export const GOODREADS_CSV_LIMIT_MESSAGE =
  'Goodreads or StoryGraph CSV exports must be 10 MB or smaller (up to 20,000 books). Choose a smaller CSV file and try again.';

export function isPayloadTooLarge(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current && typeof current === 'object'; depth++) {
    if ((current as { status?: unknown }).status === 413) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

/**
 * Maps caught upstream errors onto HTTP status codes for SvelteKit endpoints
 * and form actions:
 * - `4xx` `EngineError`s pass through unchanged.
 * - `503` `EngineError`s and non-`EngineError` exceptions (e.g. network/timeout
 *   failures) return `503` so clients and boundaries treat engine outages
 *   consistently (previously non-`EngineError`s mapped to `502`).
 * - `passthroughStatus` (such as `501` on optional SSE stream fallback) passes
 *   through when explicitly requested by the route.
 * - Other `5xx` `EngineError`s map to `502`.
 */
export function engineStatus(error: unknown, passthroughStatus?: number): number {
  if (error instanceof EngineError) {
    return error.status < 500 || error.status === passthroughStatus || error.status === 503
      ? error.status
      : 502;
  }
  return 503;
}

export async function engine<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has('content-type')) headers.set('content-type', 'application/json');
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: internalHeaders(headers),
      signal: init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(sanitizeEngineMessage(payload.detail, response.status), response.status);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
}

export async function engineStream(path: string, init?: RequestInit): Promise<Response> {
  const timeout = AbortSignal.timeout(55_000);
  const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: internalHeaders(init?.headers, 'text/event-stream'),
      signal
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(sanitizeEngineMessage(payload.detail, response.status), response.status);
  }
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')) {
    await response.body?.cancel();
    throw new EngineError('Engine did not return a Librarr event stream.', 501);
  }
  return response;
}
