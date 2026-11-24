import { once } from 'node:events';
import http from 'node:http';
import { registerHooks } from 'node:module';
import type { AddressInfo } from 'node:net';
import { isRedirect } from '@sveltejs/kit';
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { handle, isPublicApiPath, withSecurityHeaders } from './hooks.server';
import { currentSessionToken } from '#lib/server/request-context.js';
import { actions as loginActions } from './routes/login/+page.server';
import { attachOriginGuard, prepareRuntimeEnv } from '../server.js';

type AdapterResponder = (request: Request) => Promise<Response>;

const adapterBridge = globalThis as typeof globalThis & {
	__bookwardHooksSpecRespond?: AdapterResponder;
};

let adapterRunId = 0;

const ADAPTER_SHIM_SOURCE = [
	'export const server = {',
	'  init: () => Promise.resolve(),',
	'  respond: (request) => globalThis.__bookwardHooksSpecRespond(request)',
	'};',
	'export const dir = "/nonexistent";',
	'export const env_prefix = "";',
	'export const base = "";',
	'export const app_path = "_app";',
	'export const origin = undefined;',
	'export const mime_types = {};',
	'export const assets = { entries: [], aliases: [] };',
	'export const prerendered_assets = { entries: [], aliases: [] };'
].join('\n');

const ADAPTER_SHIM_URL = `data:text/javascript,${encodeURIComponent(ADAPTER_SHIM_SOURCE)}`;

registerHooks({
	resolve(specifier, context, nextResolve) {
		if (specifier === '#@sveltejs/adapter-node') {
			return { url: ADAPTER_SHIM_URL, shortCircuit: true };
		}
		if (
			specifier.startsWith('./') &&
			context.parentURL?.includes('/@sveltejs/adapter-node/src/')
		) {
			const resolved = nextResolve(specifier, context);
			return { ...resolved, url: `${resolved.url.split('?')[0]}?hooksSpecRun=${adapterRunId}` };
		}
		return nextResolve(specifier, context);
	}
});

const adapterRespond = vi.fn<AdapterResponder>();
adapterBridge.__bookwardHooksSpecRespond = (request) => adapterRespond(request);

afterEach(() => {
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
	adapterRespond.mockReset();
});

function eventFor(request: Request, session?: string) {
	return {
		url: new URL(request.url),
		request,
		locals: {},
		cookies: {
			get: (name: string) => (name === 'bookward_session' ? session : undefined),
			set: vi.fn(),
			delete: vi.fn()
		}
	} as never;
}

function mockSessionLookup(profileId = 'alice-profile') {
	const fetchMock = vi.fn().mockImplementation(
		() =>
			new Response(
				JSON.stringify({
					id: 'alice',
					profile_id: profileId,
					username: 'alice',
					display_name: 'Alice',
					role: 'user',
					must_change_password: false
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			)
	);
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

function serializeCookie(
	name: string,
	value: string,
	options: { path?: string; httpOnly?: boolean; secure?: boolean; sameSite?: string; maxAge?: number }
): string {
	const parts = [`${name}=${encodeURIComponent(value)}`];
	if (options.path) parts.push(`Path=${options.path}`);
	if (options.maxAge !== undefined) parts.push(`Max-Age=${options.maxAge}`);
	if (options.httpOnly) parts.push('HttpOnly');
	if (options.sameSite) {
		parts.push(`SameSite=${options.sameSite[0].toUpperCase()}${options.sameSite.slice(1)}`);
	}
	if (options.secure) parts.push('Secure');
	return parts.join('; ');
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

	it('never lowers Secure when refreshing session cookies unless a plain-HTTP ORIGIN is explicitly configured', async () => {
		mockSessionLookup();
		vi.stubEnv('ORIGIN', '');

		const unconfiguredHttpEvent = eventFor(new Request('http://books.example.com/'), 'valid-session');
		await handle({
			event: unconfiguredHttpEvent,
			resolve: vi.fn(() => Promise.resolve(new Response('ok')))
		} as never);
		expect(
			(unconfiguredHttpEvent as { cookies: { set: ReturnType<typeof vi.fn> } }).cookies.set
		).toHaveBeenCalledWith(
			'bookward_session',
			'valid-session',
			expect.objectContaining({ httpOnly: true, secure: true, sameSite: 'lax', path: '/' })
		);

		vi.stubEnv('ORIGIN', 'http://127.0.0.1:3000');
		const explicitHttpEvent = eventFor(new Request('http://127.0.0.1:3000/'), 'valid-session');
		await handle({
			event: explicitHttpEvent,
			resolve: vi.fn(() => Promise.resolve(new Response('ok')))
		} as never);
		expect(
			(explicitHttpEvent as { cookies: { set: ReturnType<typeof vi.fn> } }).cookies.set
		).toHaveBeenCalledWith(
			'bookward_session',
			'valid-session',
			expect.objectContaining({ httpOnly: true, secure: false, sameSite: 'lax', path: '/' })
		);

		vi.stubGlobal(
			'fetch',
			vi.fn().mockImplementation(
				() =>
					new Response(JSON.stringify({ detail: 'Invalid session' }), {
						status: 401,
						headers: { 'content-type': 'application/json' }
					})
			)
		);
		const expiredHttpsEvent = eventFor(new Request('https://books.example.com/login'), 'expired');
		await handle({
			event: expiredHttpsEvent,
			resolve: vi.fn(() => Promise.resolve(new Response('ok')))
		} as never);
		expect(
			(expiredHttpsEvent as { cookies: { delete: ReturnType<typeof vi.fn> } }).cookies.delete
		).toHaveBeenCalledWith('bookward_session', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/'
		});
	});

	it('sets and refreshes Secure session cookies on forwarded https requests with PROTOCOL_HEADER=x-forwarded-proto', async () => {
		vi.stubEnv('ORIGIN', '');
		vi.stubEnv('PROTOCOL_HEADER', 'x-forwarded-proto');
		const originConfig = prepareRuntimeEnv(process.env);

		adapterRunId += 1;
		const adapterHandlerUrl = `${new URL(
			'../node_modules/@sveltejs/adapter-node/src/handler.js',
			import.meta.url
		).href}?hooksSpecRun=${adapterRunId}`;
		const { handler } = (await import(/* @vite-ignore */ adapterHandlerUrl)) as {
			handler: (
				req: http.IncomingMessage,
				res: http.ServerResponse,
				next: () => void
			) => unknown;
		};

		vi.stubGlobal(
			'fetch',
			vi.fn().mockImplementation((input: string | URL | Request) => {
				const target = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
				if (target.endsWith('/auth/login')) {
					return new Response(
						JSON.stringify({ session_token: 'forwarded-https-session', max_age: 86400 }),
						{ status: 200, headers: { 'content-type': 'application/json' } }
					);
				}
				return new Response(
					JSON.stringify({
						id: 'alice',
						profile_id: 'alice-profile',
						username: 'alice',
						display_name: 'Alice',
						role: 'user',
						must_change_password: false
					}),
					{ status: 200, headers: { 'content-type': 'application/json' } }
				);
			})
		);

		const recordedSetOptions: Array<Record<string, unknown>> = [];
		adapterRespond.mockImplementation(async (request) => {
			const cookieHeader = request.headers.get('cookie') ?? '';
			const sessionMatch = /(?:^|;\s*)bookward_session=([^;]+)/.exec(cookieHeader);
			const incomingSession = sessionMatch ? decodeURIComponent(sessionMatch[1]) : undefined;
			const setCookies: string[] = [];
			const cookies = {
				get: (name: string) => (name === 'bookward_session' ? incomingSession : undefined),
				set: (name: string, value: string, opts: Record<string, unknown>) => {
					recordedSetOptions.push(opts);
					setCookies.push(serializeCookie(name, value, opts));
				},
				delete: (name: string, opts: Record<string, unknown>) => {
					setCookies.push(serializeCookie(name, '', { ...opts, maxAge: 0 }));
				}
			};

			const response = await handle({
				event: {
					url: new URL(request.url),
					request,
					locals: {},
					cookies
				} as never,
				resolve: async (resolvedEvent: { url: URL; request: Request; cookies: typeof cookies }) => {
					if (
						resolvedEvent.request.method === 'POST' &&
						resolvedEvent.url.pathname === '/login'
					) {
						try {
							await loginActions.login(resolvedEvent as never);
						} catch (err) {
							if (isRedirect(err)) {
								return new Response(null, {
									status: err.status,
									headers: { location: err.location }
								});
							}
							throw err;
						}
					}
					return new Response('ok', { status: 200 });
				}
			} as never);

			const headers = new Headers(response.headers);
			for (const cookie of setCookies) {
				headers.append('set-cookie', cookie);
			}
			return new Response(response.body, { status: response.status, headers });
		});

		const server = attachOriginGuard(
			http.createServer((req, res) =>
				handler(req, res, () => {
					res.statusCode = 404;
					res.end();
				})
			),
			originConfig
		);
		onTestFinished(async () => {
			server.closeAllConnections?.();
			await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
		});
		server.listen(0, '127.0.0.1');
		await once(server, 'listening');
		const { port } = server.address() as AddressInfo;

		const sendForwarded = (options: {
			method: string;
			path: string;
			headers: Record<string, string>;
			body?: string;
		}) =>
			new Promise<{ status: number; setCookie: string }>((resolveRes, rejectRes) => {
				const payload = options.body ? Buffer.from(options.body, 'utf8') : undefined;
				const req = http.request(
					{
						host: '127.0.0.1',
						port,
						method: options.method,
						path: options.path,
						headers: {
							...(payload ? { 'content-length': String(payload.byteLength) } : {}),
							...options.headers
						}
					},
					(res) => {
						res.resume();
						res.on('end', () => {
							resolveRes({
								status: res.statusCode ?? 0,
								setCookie: [res.headers['set-cookie'] ?? []].flat().join('; ')
							});
						});
					}
				);
				req.on('error', rejectRes);
				if (payload) req.write(payload);
				req.end();
			});

		const loginForm = new URLSearchParams({
			username: 'alice',
			password: 'Valid-login-password-1'
		}).toString();
		const loginRes = await sendForwarded({
			method: 'POST',
			path: '/login?/login',
			headers: {
				host: 'books.example.com',
				origin: 'https://books.example.com',
				'x-forwarded-proto': 'https',
				'content-type': 'application/x-www-form-urlencoded'
			},
			body: loginForm
		});
		expect(loginRes.status).toBe(303);
		expect(loginRes.setCookie).toContain('bookward_session=forwarded-https-session');
		expect(loginRes.setCookie).toContain('Secure');

		const refreshRes = await sendForwarded({
			method: 'GET',
			path: '/',
			headers: {
				host: 'books.example.com',
				'x-forwarded-proto': 'https',
				cookie: 'bookward_session=forwarded-https-session'
			}
		});
		expect(refreshRes.status).toBe(200);
		expect(refreshRes.setCookie).toContain('bookward_session=forwarded-https-session');
		expect(refreshRes.setCookie).toContain('Secure');
		expect(recordedSetOptions).toEqual([
			expect.objectContaining({ httpOnly: true, secure: true, sameSite: 'lax', path: '/' }),
			expect.objectContaining({ httpOnly: true, secure: true, sameSite: 'lax', path: '/' })
		]);
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

	it('rejects cross-profile form actions and profile-bound GET fetches with HTTP 409', async () => {
		mockSessionLookup('alice-profile');
		const form = new URLSearchParams({ __profile_id: 'bob-profile', id: '15' });
		const formMismatch = await handle({
			event: eventFor(new Request('http://afterword.test/?/updateRead', {
				method: 'POST',
				headers: {
					origin: 'http://afterword.test',
					'content-type': 'application/x-www-form-urlencoded'
				},
				body: form.toString()
			}), 'valid-session'),
			resolve: vi.fn()
		} as never);
		expect(formMismatch.status).toBe(409);

		const sliceMismatch = await handle({
			event: eventFor(new Request('http://afterword.test/api/recommendations?status=maybe_later&limit=24&offset=0', {
				headers: { 'x-bookward-profile': 'bob-profile' }
			}), 'valid-session'),
			resolve: vi.fn()
		} as never);
		expect(sliceMismatch.status).toBe(409);
	});

	it('parses multipart uploads once without cloning the request and reuses formData in actions', async () => {
		mockSessionLookup();
		const form = new FormData();
		form.set('__profile_id', 'alice-profile');
		form.set(
			'file',
			new File(['Title,Author\nDune,Frank Herbert\n'], 'goodreads.csv', { type: 'text/csv' })
		);
		const request = new Request('http://afterword.test/?/importGoodreadsCsv', {
			method: 'POST',
			headers: {
				origin: 'http://afterword.test',
				accept: 'application/json',
				'x-sveltekit-action': 'true'
			},
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
		wrongProfileForm.set(
			'file',
			new File(['Title,Author\n'], 'goodreads.csv', { type: 'text/csv' })
		);
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
					controller.error(
						Object.assign(new Error('Content-length exceeds limit'), { status: 413 })
					);
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
		const payload = (await jsonResponse.json()) as { detail: string; message: string };
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
