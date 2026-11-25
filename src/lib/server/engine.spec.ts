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

	it('distinguishes 401 authentication errors from upstream unavailability and maps engineStatus branches', () => {
		const unauthorized = new EngineError('Authentication required', 401);
		const notImplemented = new EngineError('Stream unsupported', 501);
		const badGateway = new EngineError('Upstream failed', 502);
		const internalError = new EngineError('Internal failure', 500);
		const unavailable = new EngineError(ENGINE_UNAVAILABLE_MESSAGE, 503, true);

		expect(isEngineUnavailable(unauthorized)).toBe(false);
		expect(engineStatus(unauthorized)).toBe(401);
		expect(engineStatus(notImplemented, 501)).toBe(501);
		expect(engineStatus(notImplemented)).toBe(502);
		expect(isEngineUnavailable(badGateway)).toBe(true);
		expect(engineStatus(badGateway)).toBe(502);
		expect(engineStatus(internalError)).toBe(502);
		expect(engineStatus(unavailable)).toBe(503);
		expect(engineStatus(new TypeError('network down'))).toBe(503);
	});

	it('redacts service secrets, tokens, URL credentials, local paths, and Python tracebacks from error messages', () => {
		const serviceSecret = ['svc', 'fixture', 'redacted'].join('-');
		const signingSecret = ['app', 'fixture', 'redacted'].join('-');
		const apiToken = ['bkw', 'fixture', 'tok99'].join('_');
		const bearerHeader = ['Bearer', 'fixture-opaque-value'].join(' ');
		const credUrl = ['https://reader', 'fixture-cred@goodreads.example/feed'].join(':');

		vi.stubEnv('ENGINE_SERVICE_SECRET', serviceSecret);
		vi.stubEnv('AFTERWORD_SECRET_KEY', signingSecret);

		expect(
			sanitizeEngineMessage(`Header mismatch for ${serviceSecret} and ${signingSecret}`, 403)
		).toBe('Header mismatch for [redacted] and [redacted]');
		expect(
			sanitizeEngineMessage(`Token ${apiToken} or ${bearerHeader} failed on ${credUrl}`, 400)
		).toBe('Token [redacted] or Bearer [redacted] failed on https://[redacted]@goodreads.example/feed');
		expect(
			sanitizeEngineMessage('sqlite3.OperationalError: unable to open /var/lib/bookward/engine.db-wal', 500)
		).toBe(ENGINE_UNAVAILABLE_MESSAGE);
		expect(
			sanitizeEngineMessage('Traceback (most recent call last):\n  File "/app/main.py", line 12\nRuntimeError', 500)
		).toBe(ENGINE_UNAVAILABLE_MESSAGE);
		expect(
			sanitizeEngineMessage('PermissionError: [Errno 13] Permission denied: /etc/bookward/config.toml', 400)
		).toBe('The request could not be completed.');
		expect(
			sanitizeEngineMessage('Failed reading C:\\Users\\admin\\afterword.sqlite', 422)
		).toBe('The request could not be completed.');
		expect(
			sanitizeEngineMessage('Choose a text/csv file from https://www.goodreads.com/review/import', 400)
		).toBe('Choose a text/csv file from https://www.goodreads.com/review/import');
	});

	it('fails closed on raw 5xx details while preserving safe recovery messages and 502 integration prefixes', () => {
		expect(sanitizeEngineMessage('KeyError: "profile_id"', 500)).toBe(ENGINE_UNAVAILABLE_MESSAGE);
		expect(sanitizeEngineMessage('Unexpected worker failure in recommendation pipeline', 500)).toBe(
			ENGINE_UNAVAILABLE_MESSAGE
		);
		expect(sanitizeEngineMessage('Bad Gateway: upstream 10.0.0.14:8000 connection reset', 502)).toBe(
			ENGINE_UNAVAILABLE_MESSAGE
		);
		expect(sanitizeEngineMessage('Database pool exhausted', 503)).toBe(ENGINE_UNAVAILABLE_MESSAGE);

		expect(
			sanitizeEngineMessage(
				'Profile database is unavailable because an interrupted restore has not been recovered. Restore a valid backup or ask an administrator to clear the restore journal.',
				503
			)
		).toContain('interrupted restore');
		expect(
			sanitizeEngineMessage('API token verification is busy. Try again shortly.', 503)
		).toBe('API token verification is busy. Try again shortly.');
		expect(sanitizeEngineMessage('Librarr search failed: HTTP 401 Unauthorized', 502)).toBe(
			'Librarr search failed: HTTP 401 Unauthorized'
		);
		expect(sanitizeEngineMessage('Unable to fetch models.dev catalog: timed out', 502)).toBe(
			'Unable to fetch models.dev catalog: timed out'
		);
		expect(sanitizeEngineMessage('The SSO provider could not be reached', 502)).toBe(
			'The SSO provider could not be reached'
		);
		expect(sanitizeEngineMessage('Google Books preview failed: HTTP 503', 502)).toBe(
			'Google Books preview failed: HTTP 503'
		);
		expect(
			sanitizeEngineMessage('Librarr request failed: FileNotFoundError /var/run/librarr.sock', 502)
		).toBe(ENGINE_UNAVAILABLE_MESSAGE);
	});
});
