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

export function sanitizeEngineMessage(detail: string | undefined, status: number): string {
  const fallback = status >= 500 ? ENGINE_UNAVAILABLE_MESSAGE : `Engine returned ${status}`;
  if (!detail || !detail.trim()) return fallback;
  let cleaned = detail.trim();
  const secret = env.ENGINE_SERVICE_SECRET?.trim();
  if (secret && cleaned.includes(secret)) {
    cleaned = cleaned.replaceAll(secret, '[redacted]');
  }
  const hasStackTrace =
    cleaned.includes('Traceback (most recent call last)') ||
    /\n\s*File "[^"]+", line \d+/.test(cleaned) ||
    /\n\s+at\s+\S+/.test(cleaned);
  const hasSqliteLeak =
    /\bsqlite3\./i.test(cleaned) ||
    /(?:\/|[A-Za-z]:\\)\S*\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?\b/i.test(cleaned);
  if (hasStackTrace || hasSqliteLeak) {
    return status >= 500 ? ENGINE_UNAVAILABLE_MESSAGE : 'The request could not be completed.';
  }
  return cleaned;
}

export const GOODREADS_CSV_MAX_BYTES = 10_000_000;
export const GOODREADS_CSV_LIMIT_MESSAGE =
  'Goodreads CSV exports must be 10 MB or smaller (up to 20,000 books). Choose a smaller CSV file and try again.';

export function isPayloadTooLarge(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current && typeof current === 'object'; depth++) {
    if ((current as { status?: unknown }).status === 413) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

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
