import { env } from '$env/dynamic/private';

const base = (env.ENGINE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
export class EngineError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export async function engine<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { accept: 'application/json', ...(init?.body instanceof FormData ? {} : { 'content-type': 'application/json' }), ...init?.headers },
    signal: AbortSignal.timeout(30_000)
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new EngineError(payload.detail || `Engine returned ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}
