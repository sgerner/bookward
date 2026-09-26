import { env } from '$env/dynamic/private';

const base = (env.ENGINE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
export class EngineError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export async function engine<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { accept: 'application/json', ...(init?.body instanceof FormData ? {} : { 'content-type': 'application/json' }), ...init?.headers },
    signal: init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(payload.detail || `Engine returned ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export async function engineStream(path: string, init?: RequestInit): Promise<Response> {
  const timeout = AbortSignal.timeout(55_000);
  const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { accept: 'text/event-stream', ...init?.headers },
    signal
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(payload.detail || `Engine returned ${response.status}`, response.status);
  }
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')) {
    await response.body?.cancel();
    throw new EngineError('Engine did not return a Librarr event stream.', 501);
  }
  return response;
}
