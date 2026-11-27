const env = process.env;
import { currentClientIp, currentSessionToken } from '#lib/server/request-context.js';

const base = (env.ENGINE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');

export function validateClientIpConfig(
  runtimeEnv: NodeJS.ProcessEnv = process.env,
  options: { warn?: (message: string) => void } = {}
): void {
  const addressHeader = runtimeEnv.ADDRESS_HEADER?.trim() || '';
  const serviceSecret = runtimeEnv.ENGINE_SERVICE_SECRET?.trim() || '';
  if (addressHeader && !serviceSecret) {
    throw new Error(
      'ENGINE_SERVICE_SECRET must be set when ADDRESS_HEADER is configured so the engine trusts forwarded client IPs.'
    );
  }
  if (addressHeader.toLowerCase() === 'x-forwarded-for') {
    const rawDepth = runtimeEnv.XFF_DEPTH?.trim();
    if (rawDepth !== undefined && rawDepth !== '' && !/^[1-9]\d*$/.test(rawDepth)) {
      throw new Error('XFF_DEPTH must be a positive integer when ADDRESS_HEADER=x-forwarded-for.');
    }
  }
  const warn = options.warn;
  if (warn) {
    if (!serviceSecret) {
      warn(
        '[bookward] ENGINE_SERVICE_SECRET is not set; the engine ignores forwarded client IPs and sign-in rate limiting shares one peer IP bucket.'
      );
    } else if (!addressHeader) {
      warn(
        '[bookward] ADDRESS_HEADER is not set; sign-in rate limiting uses direct socket addresses (behind a reverse proxy, all clients share the proxy IP bucket until ADDRESS_HEADER and XFF_DEPTH are configured).'
      );
    }
  }
}

function internalHeaders(headers?: HeadersInit, accept = 'application/json') {
  validateClientIpConfig(env);
  const merged = new Headers(headers);
  merged.set('accept', accept);
  merged.delete('x-bookward-service');
  merged.delete('x-bookward-client-ip');
  if (env.ENGINE_SERVICE_SECRET?.trim()) merged.set('x-bookward-service', env.ENGINE_SERVICE_SECRET);
  const session = currentSessionToken();
  if (session) merged.set('x-bookward-session', session);
  const clientIp = currentClientIp();
  if (clientIp) merged.set('x-bookward-client-ip', clientIp);
  return merged;
}
export const ENGINE_UNAVAILABLE_MESSAGE = "Bookward's recommendation engine is unavailable.";

function parseRetryAfter(headers: Headers): number | undefined {
  const raw = headers.get('retry-after')?.trim();
  if (!raw || !/^\d+$/.test(raw)) return undefined;
  const parsed = Number.parseInt(raw, 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : undefined;
}

export class EngineError extends Error {
  readonly retryAfterSeconds?: number;
  readonly unavailable: boolean;

  constructor(
    message: string,
    readonly status: number,
    retryAfterOrUnavailable?: number | boolean,
    unavailable?: boolean
  ) {
    super(message);
    if (typeof retryAfterOrUnavailable === 'number') {
      this.retryAfterSeconds = retryAfterOrUnavailable;
      this.unavailable = unavailable ?? status >= 500;
    } else {
      this.unavailable = retryAfterOrUnavailable ?? unavailable ?? status >= 500;
    }
  }
}

export function isEngineUnavailable(error: unknown): boolean {
  return !(error instanceof EngineError) || error.unavailable || error.status >= 500;
}

const SAFE_EXACT_5XX_MESSAGES = new Set([
  'Profile database is unavailable because an interrupted restore has not been recovered. Restore a valid backup or ask an administrator to clear the restore journal.',
  'API token verification is busy. Try again shortly.',
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

export async function engineWithHeaders<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 30_000,
): Promise<{ data: T; headers: Headers }> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has('content-type')) headers.set('content-type', 'application/json');
  const requestHeaders = internalHeaders(headers);
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: requestHeaders,
      signal: init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(
      sanitizeEngineMessage(payload.detail, response.status),
      response.status,
      parseRetryAfter(response.headers)
    );
  }
  let data: T;
  try {
    data = (await response.json()) as T;
  } catch {
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
  return {
    data,
    headers: response.headers,
  };
}

export async function engine<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const { data } = await engineWithHeaders<T>(path, init, timeoutMs);
  return data;
}

export async function engineStream(path: string, init?: RequestInit): Promise<Response> {
  const timeout = AbortSignal.timeout(55_000);
  const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  const requestHeaders = internalHeaders(init?.headers, 'text/event-stream');
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: requestHeaders,
      signal
    });
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(
      sanitizeEngineMessage(payload.detail, response.status),
      response.status,
      parseRetryAfter(response.headers)
    );
  }
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')) {
    await response.body?.cancel();
    throw new EngineError('Engine did not return a Librarr event stream.', 501);
  }
  return response;
}
