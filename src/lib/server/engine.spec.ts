import { afterEach, describe, expect, it, vi } from 'vitest';
import { EngineError, engine, validateClientIpConfig } from './engine';
import { withRequestContext, withSessionToken } from './request-context';

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

	it('forwards verified client IPs from request context and strips spoofed headers', async () => {
		const fetchMock = vi.fn().mockImplementation(() =>
			Promise.resolve(
				new Response(JSON.stringify({ ok: true }), {
					status: 200,
					headers: { 'content-type': 'application/json' }
				})
			)
		);
		vi.stubGlobal('fetch', fetchMock);

		await withRequestContext({ clientIp: '198.51.100.42' }, () =>
			engine('/auth/login', {
				method: 'POST',
				headers: { 'x-bookward-client-ip': '203.0.113.9' },
				body: JSON.stringify({ username: 'reader', password: 'secret' })
			})
		);

		let [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		let headers = new Headers(init.headers);
		expect(headers.get('x-bookward-client-ip')).toBe('198.51.100.42');

		await engine('/auth/config', {
			headers: {
				'x-bookward-client-ip': '203.0.113.9',
				'x-bookward-service': 'forged-secret'
			}
		});
		[, init] = fetchMock.mock.calls[1] as [string, RequestInit];
		headers = new Headers(init.headers);
		expect(headers.get('x-bookward-client-ip')).toBeNull();
		expect(headers.get('x-bookward-service')).toBeNull();
	});

	it('captures Retry-After seconds on HTTP 429 EngineError responses', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(
				new Response(JSON.stringify({ detail: 'Too many failed password attempts.' }), {
					status: 429,
					headers: { 'content-type': 'application/json', 'retry-after': '840' }
				})
			)
		);

		await expect(engine('/auth/login', { method: 'POST', body: '{}' })).rejects.toMatchObject({
			status: 429,
			retryAfterSeconds: 840,
			message: 'Too many failed password attempts.'
		} satisfies Partial<EngineError>);
	});

	it('requires ENGINE_SERVICE_SECRET when ADDRESS_HEADER is set and warns on shared-bucket defaults', async () => {
		expect(() =>
			validateClientIpConfig({ ADDRESS_HEADER: 'x-forwarded-for', ENGINE_SERVICE_SECRET: '' })
		).toThrow(/ENGINE_SERVICE_SECRET must be set/);

		expect(() =>
			validateClientIpConfig({
				ADDRESS_HEADER: 'x-forwarded-for',
				ENGINE_SERVICE_SECRET: 'service-secret',
				XFF_DEPTH: '0'
			})
		).toThrow(/XFF_DEPTH must be a positive integer/);

		const warnings: string[] = [];
		validateClientIpConfig(
			{ ADDRESS_HEADER: '', ENGINE_SERVICE_SECRET: '' },
			{ warn: (msg) => warnings.push(msg) }
		);
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain('ENGINE_SERVICE_SECRET is not set');

		warnings.length = 0;
		validateClientIpConfig(
			{ ADDRESS_HEADER: '', ENGINE_SERVICE_SECRET: 'service-secret' },
			{ warn: (msg) => warnings.push(msg) }
		);
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain('ADDRESS_HEADER is not set');

		warnings.length = 0;
		validateClientIpConfig(
			{ ADDRESS_HEADER: 'x-forwarded-for', ENGINE_SERVICE_SECRET: 'service-secret', XFF_DEPTH: '2' },
			{ warn: (msg) => warnings.push(msg) }
		);
		expect(warnings).toHaveLength(0);

		vi.stubEnv('ADDRESS_HEADER', 'x-forwarded-for');
		vi.stubEnv('ENGINE_SERVICE_SECRET', '');
		await expect(engine('/auth/config')).rejects.toThrow(/ENGINE_SERVICE_SECRET must be set/);
	});
});
