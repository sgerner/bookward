import { afterEach, describe, expect, it, vi } from 'vitest';
import { authorized, handle, isPublicApiPath, withSecurityHeaders } from './hooks.server';
import { currentSessionToken } from '$lib/server/request-context';

afterEach(() => vi.unstubAllGlobals());

function eventFor(request: Request, session?: string) {
	return {
		url: new URL(request.url),
		request,
		locals: {},
		cookies: {
			get: (name: string) => name === 'bookward_session' ? session : undefined,
			set: vi.fn(),
			delete: vi.fn()
		}
	} as never;
}

function mockSessionLookup(profileId = 'alice-profile') {
	const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({
		id: 'alice', profile_id: profileId, username: 'alice', display_name: 'Alice',
		role: 'user', must_change_password: false
	}), { status: 200, headers: { 'content-type': 'application/json' } }));
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

describe('authentication boundary', () => {
  it('requires configured basic authentication and accepts colons in passwords', async () => {
    expect(authorized(new Request('http://afterword.test'), 'reader', 'long:secret')).toBe(false);
    const authorization = `Basic ${Buffer.from('reader:long:secret').toString('base64')}`;
    expect(authorized(new Request('http://afterword.test', { headers: { authorization } }), 'reader', 'long:secret')).toBe(true);
  });

  it('recognizes only the versioned API paths as token-authenticated routes', () => {
    expect(isPublicApiPath('/api/v1')).toBe(true);
    expect(isPublicApiPath('/api/v1/recommendations')).toBe(true);
    expect(isPublicApiPath('/api/v1/%72ecommendations')).toBe(true);
    expect(isPublicApiPath('/api/v1/%2e%2e/settings')).toBe(false);
    expect(isPublicApiPath('/api/v1/%5c%2e%2e%5csettings')).toBe(false);
    expect(isPublicApiPath('/api/v10/recommendations')).toBe(false);
    expect(isPublicApiPath('/settings')).toBe(false);
  });

  it('adds baseline browser security headers to every response', () => {
    const response = withSecurityHeaders(new Response('ok'));

    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
    expect(response.headers.get('x-frame-options')).toBe('DENY');
    expect(response.headers.get('referrer-policy')).toBe('strict-origin-when-cross-origin');
    expect(response.headers.get('permissions-policy')).toBe('camera=(), geolocation=(), microphone=()');
  });

	it('denies private API routes without a browser session', async () => {
		const request = new Request('http://afterword.test/api/telemetry');
		const response = await handle({
			event: eventFor(request),
			resolve: vi.fn()
		} as never);

		expect(response.status).toBe(401);
	});

	it('rejects cross-origin mutations before resolving the session', async () => {
		const fetchMock = mockSessionLookup();
		const request = new Request('http://afterword.test/api/telemetry', {
			method: 'POST',
			headers: { origin: 'https://attacker.test', 'content-type': 'application/json' },
			body: JSON.stringify({ expected_profile_id: 'alice-profile', events: [] })
		});
		const response = await handle({
			event: eventFor(request, 'valid-session'),
			resolve: vi.fn()
		} as never);

		expect(response.status).toBe(403);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('binds authenticated mutations to the current profile and request session', async () => {
		mockSessionLookup();
		const request = new Request('http://afterword.test/api/telemetry', {
			method: 'POST',
			headers: { origin: 'http://afterword.test', 'content-type': 'application/json' },
			body: JSON.stringify({ expected_profile_id: 'other-profile', events: [] })
		});
		const mismatch = await handle({
			event: eventFor(request, 'valid-session'),
			resolve: vi.fn()
		} as never);
		expect(mismatch.status).toBe(409);

		const validRequest = new Request('http://afterword.test/api/telemetry', {
			method: 'POST',
			headers: { origin: 'http://afterword.test', 'content-type': 'application/json' },
			body: JSON.stringify({ expected_profile_id: 'alice-profile', events: [] })
		});
		let sessionInRoute: string | undefined;
		const response = await handle({
			event: eventFor(validRequest, 'valid-session'),
			resolve: vi.fn(() => {
				sessionInRoute = currentSessionToken();
				return Promise.resolve(new Response('ok'));
			})
		} as never);
		expect(response.status).toBe(200);
		expect(sessionInRoute).toBe('valid-session');
	});
});
