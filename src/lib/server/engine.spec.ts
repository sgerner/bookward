import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	ENGINE_UNAVAILABLE_MESSAGE,
	EngineError,
	engine,
	engineStatus,
	isEngineUnavailable,
	sanitizeEngineMessage
} from './engine';
import { withSessionToken } from './request-context';

afterEach(() => {
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
});

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

	it('classifies upstream connection failures as 503 EngineError without leaking low-level fetch details', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed: ECONNREFUSED 127.0.0.1:8000')));

		let caught: unknown;
		try {
			await engine('/auth/me');
		} catch (error) {
			caught = error;
		}

		expect(caught).toBeInstanceOf(EngineError);
		expect((caught as EngineError).status).toBe(503);
		expect((caught as EngineError).message).toBe(ENGINE_UNAVAILABLE_MESSAGE);
		expect(isEngineUnavailable(caught)).toBe(true);
		expect(engineStatus(caught)).toBe(503);
	});

	it('distinguishes 401 authentication errors from upstream unavailability', () => {
		const unauthorized = new EngineError('Authentication required', 401);
		const badGateway = new EngineError('Upstream failed', 502);

		expect(isEngineUnavailable(unauthorized)).toBe(false);
		expect(engineStatus(unauthorized)).toBe(401);
		expect(isEngineUnavailable(badGateway)).toBe(true);
		expect(engineStatus(badGateway)).toBe(502);
	});

	it('redacts service secrets, SQLite file paths, and Python tracebacks from error messages', () => {
		vi.stubEnv('ENGINE_SERVICE_SECRET', 'super-secret-token');

		expect(
			sanitizeEngineMessage('Header mismatch for super-secret-token', 403)
		).toBe('Header mismatch for [redacted]');
		expect(
			sanitizeEngineMessage('sqlite3.OperationalError: unable to open /var/lib/bookward/engine.db-wal', 500)
		).toBe(ENGINE_UNAVAILABLE_MESSAGE);
		expect(
			sanitizeEngineMessage('Traceback (most recent call last):\n  File "/app/main.py", line 12\nRuntimeError', 500)
		).toBe(ENGINE_UNAVAILABLE_MESSAGE);
	});
});
