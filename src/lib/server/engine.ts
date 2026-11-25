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
function parseRetryAfter(headers: Headers): number | undefined {
  const raw = headers.get('retry-after')?.trim();
  if (!raw || !/^\d+$/.test(raw)) return undefined;
  const parsed = Number.parseInt(raw, 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : undefined;
}
export class EngineError extends Error {
  constructor(message: string, readonly status: number, readonly retryAfterSeconds?: number) {
    super(message);
  }
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

export function engineStatus(error: unknown, passthroughStatus?: number): number {
  return error instanceof EngineError && (error.status < 500 || error.status === passthroughStatus)
    ? error.status
    : 502;
}

export async function engine<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has('content-type')) headers.set('content-type', 'application/json');
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: internalHeaders(headers),
    signal: init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(
      payload.detail || `Engine returned ${response.status}`,
      response.status,
      parseRetryAfter(response.headers)
    );
  }
  return response.json() as Promise<T>;
}

export async function engineStream(path: string, init?: RequestInit): Promise<Response> {
  const timeout = AbortSignal.timeout(55_000);
  const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: internalHeaders(init?.headers, 'text/event-stream'),
    signal
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(
      payload.detail || `Engine returned ${response.status}`,
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
