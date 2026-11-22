import { afterEach, describe, expect, it, vi } from 'vitest';
import { handle, isPublicApiPath, withSecurityHeaders } from './hooks.server';
import { currentSessionToken } from '#lib/server/request-context.js';

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

	it('returns both detail and message on 401, 403, 409, and 503 JSON responses', async () => {
		const unauthResponse = await handle({
			event: eventFor(new Request('http://afterword.test/api/recommendations')),
			resolve: vi.fn()
		} as never);
		expect(unauthResponse.status).toBe(401);
		const unauthPayload = await unauthResponse.json();
		expect(unauthPayload.detail).toContain('Authentication required');
		expect(unauthPayload.message).toBe(unauthPayload.detail);

		const crossOriginResponse = await handle({
			event: eventFor(
				new Request('http://afterword.test/api/telemetry', {
					method: 'POST',
					headers: { origin: 'https://attacker.test', 'content-type': 'application/json' },
					body: '{}'
				}),
				'valid-session'
			),
			resolve: vi.fn()
		} as never);
		expect(crossOriginResponse.status).toBe(403);
		const crossOriginPayload = await crossOriginResponse.json();
		expect(crossOriginPayload.message).toBe(crossOriginPayload.detail);

		mockSessionLookup('alice-profile');
		const conflictResponse = await handle({
			event: eventFor(
				new Request('http://afterword.test/api/recommendations', {
					headers: { 'x-bookward-profile': 'bob-profile' }
				}),
				'valid-session'
			),
			resolve: vi.fn()
		} as never);
		expect(conflictResponse.status).toBe(409);
		const conflictPayload = await conflictResponse.json();
		expect(conflictPayload.detail).toContain('different profile');
		expect(conflictPayload.message).toBe(conflictPayload.detail);

		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));
		const outageApiEvent = eventFor(new Request('http://afterword.test/api/recommendations'), 'valid-session');
		const outageResponse = await handle({
			event: outageApiEvent,
			resolve: vi.fn()
		} as never);
		expect(outageResponse.status).toBe(503);
		const outagePayload = await outageResponse.json();
		expect(outagePayload.message).toBe(outagePayload.detail);
		expect((outageApiEvent as { cookies: { delete: ReturnType<typeof vi.fn> } }).cookies.delete).not.toHaveBeenCalled();
	});

	it('retains bookward_session and passes locals.engineUnavailable on page loads during engine outages', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));

		for (const pathname of ['/', '/login', '/account', '/admin']) {
			const event = eventFor(new Request(`http://afterword.test${pathname}`), 'valid-session') as {
				locals: App.Locals;
				cookies: { delete: ReturnType<typeof vi.fn> };
			};
			const resolve = vi.fn().mockResolvedValue(new Response('<main id="main-content"></main>', { status: 503 }));
			const response = await handle({ event: event as never, resolve } as never);

			expect(response.status).toBe(503);
			expect(event.locals.engineUnavailable).toBe(true);
			expect(event.cookies.delete).not.toHaveBeenCalled();
			expect(resolve).toHaveBeenCalledTimes(1);
		}
	});

	it('fails closed with HTTP 503 on mutations during engine outages without clearing the session cookie', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));
		const event = eventFor(
			new Request('http://afterword.test/?/decide', {
				method: 'POST',
				headers: {
					origin: 'http://afterword.test',
					accept: 'application/json',
					'x-sveltekit-action': 'true',
					'content-type': 'application/x-www-form-urlencoded'
				},
				body: new URLSearchParams({ __profile_id: 'alice-profile', id: '7', status: 'saved' })
			}),
			'valid-session'
		) as { cookies: { delete: ReturnType<typeof vi.fn> } };
		const resolve = vi.fn();

		const response = await handle({ event: event as never, resolve } as never);
		expect(response.status).toBe(503);
		expect(resolve).not.toHaveBeenCalled();
		expect(event.cookies.delete).not.toHaveBeenCalled();
	});

	it('renders HTML with <main> on unhydrated 409 form posts while keeping the session active', async () => {
		mockSessionLookup('alice-profile');
		const event = eventFor(
			new Request('http://afterword.test/auth/logout', {
				method: 'POST',
				headers: {
					origin: 'http://afterword.test',
					accept: 'text/html,application/xhtml+xml',
					'content-type': 'application/x-www-form-urlencoded'
				},
				body: new URLSearchParams({ __profile_id: 'stale-profile' })
			}),
			'valid-session'
		) as { cookies: { delete: ReturnType<typeof vi.fn> } };
		const resolve = vi.fn();

		const response = await handle({ event: event as never, resolve } as never);
		const body = await response.text();

		expect(response.status).toBe(409);
		expect(response.headers.get('content-type')).toContain('text/html');
		expect(body).toContain('<main id="main-content"');
		expect(body).toContain('This page belongs to a different profile. Reload before continuing.');
		expect(resolve).not.toHaveBeenCalled();
		expect(event.cookies.delete).not.toHaveBeenCalled();
	});
});
