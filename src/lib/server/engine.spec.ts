import { afterEach, describe, expect, it, vi } from 'vitest';
import { engine } from './engine';
import { withSessionToken } from './request-context';

afterEach(() => vi.unstubAllGlobals());

describe('private engine client', () => {
	it('uses the request session instead of a caller-provided session header', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ ok: true }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		await withSessionToken('server-session-token', () =>
			engine('/api/overview', {
				headers: { 'x-bookward-session': 'client-controlled-token' }
			})
		);

		const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		const headers = new Headers(init.headers);
		expect(headers.get('x-bookward-session')).toBe('server-session-token');
	});
});
