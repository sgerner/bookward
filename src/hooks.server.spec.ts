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
		await expect(response.json()).resolves.toEqual({
			detail: 'Request origin could not be verified',
			message: 'Request origin could not be verified'
		});
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
		await expect(mismatch.json()).resolves.toEqual({
			detail: 'This page belongs to a different profile. Reload before continuing.',
			message: 'This page belongs to a different profile. Reload before continuing.'
		});

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

	it('parses multipart uploads once without cloning the request and reuses formData in actions', async () => {
		mockSessionLookup();
		const form = new FormData();
		form.set('__profile_id', 'alice-profile');
		form.set('file', new File(['Title,Author\nDune,Frank Herbert\n'], 'goodreads.csv', { type: 'text/csv' }));
		const request = new Request('http://afterword.test/?/importGoodreadsCsv', {
			method: 'POST',
			headers: { origin: 'http://afterword.test', accept: 'application/json', 'x-sveltekit-action': 'true' },
			body: form
		});
		const cloneSpy = vi.spyOn(request, 'clone');
		const nativeFormData = request.formData.bind(request);
		const formDataSpy = vi.spyOn(request, 'formData').mockImplementation(() => nativeFormData());

		let actionFileText = '';
		const response = await handle({
			event: eventFor(request, 'valid-session'),
			resolve: vi.fn(async (resolvedEvent: { request: Request }) => {
				const parsed = await resolvedEvent.request.formData();
				const uploaded = parsed.get('file');
				actionFileText = uploaded instanceof File ? await uploaded.text() : '';
				return new Response('ok');
			})
		} as never);

		expect(response.status).toBe(200);
		expect(cloneSpy).not.toHaveBeenCalled();
		expect(formDataSpy).toHaveBeenCalledTimes(1);
		expect(actionFileText).toContain('Dune,Frank Herbert');

		const wrongProfileForm = new FormData();
		wrongProfileForm.set('__profile_id', 'other-profile');
		wrongProfileForm.set('file', new File(['Title,Author\n'], 'goodreads.csv', { type: 'text/csv' }));
		const mismatch = await handle({
			event: eventFor(
				new Request('http://afterword.test/?/importGoodreadsCsv', {
					method: 'POST',
					headers: { origin: 'http://afterword.test', accept: 'application/json' },
					body: wrongProfileForm
				}),
				'valid-session'
			),
			resolve: vi.fn()
		} as never);
		expect(mismatch.status).toBe(409);
	});

	it('returns HTTP 413 instead of 409 when multipart parsing hits BODY_SIZE_LIMIT', async () => {
		mockSessionLookup();
		const oversizedStream = () =>
			new ReadableStream({
				start(controller) {
					controller.error(Object.assign(new Error('Content-length exceeds limit'), { status: 413 }));
				}
			});

		const jsonRequest = new Request('http://afterword.test/?/importGoodreadsCsv', {
			method: 'POST',
			// @ts-expect-error Node fetch requires half-duplex for streaming request bodies
			duplex: 'half',
			headers: {
				origin: 'http://afterword.test',
				accept: 'application/json',
				'x-sveltekit-action': 'true',
				'content-type': 'multipart/form-data; boundary=----bookward'
			},
			body: oversizedStream()
		});
		const jsonResponse = await handle({
			event: eventFor(jsonRequest, 'valid-session'),
			resolve: vi.fn()
		} as never);

		expect(jsonResponse.status).toBe(413);
		expect(jsonResponse.headers.get('x-content-type-options')).toBe('nosniff');
		const payload = await jsonResponse.json() as { detail: string; message: string };
		expect(payload.detail).toContain('10 MB');
		expect(payload.message).toBe(payload.detail);

		const htmlRequest = new Request('http://afterword.test/?/importGoodreadsCsv', {
			method: 'POST',
			// @ts-expect-error Node fetch requires half-duplex for streaming request bodies
			duplex: 'half',
			headers: {
				origin: 'http://afterword.test',
				accept: 'text/html,application/xhtml+xml',
				'content-type': 'multipart/form-data; boundary=----bookward'
			},
			body: oversizedStream()
		});
		await expect(
			handle({
				event: eventFor(htmlRequest, 'valid-session'),
				resolve: vi.fn()
			} as never)
		).rejects.toMatchObject({
			status: 413,
			body: { message: expect.stringContaining('10 MB') }
		});
	});
});
