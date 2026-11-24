import { once } from 'node:events';
import http from 'node:http';
import { registerHooks } from 'node:module';
import type { AddressInfo } from 'node:net';
import process from 'node:process';
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { handle } from './hooks.server';
import { prepareRuntimeEnv, startServer, wrapAdapterHandler } from '../server.js';

type AdapterResponder = (request: Request) => Promise<Response>;

const adapterBridge = globalThis as typeof globalThis & {
	__bookwardServerSpecRespond?: AdapterResponder;
};

let adapterRunId = 0;

const ADAPTER_SHIM_SOURCE = [
	'export const server = {',
	'  init: () => Promise.resolve(),',
	'  respond: (request) => globalThis.__bookwardServerSpecRespond(request)',
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
			return { ...resolved, url: `${resolved.url.split('?')[0]}?serverSpecRun=${adapterRunId}` };
		}
		return nextResolve(specifier, context);
	}
});

const respond = vi.fn<AdapterResponder>();
adapterBridge.__bookwardServerSpecRespond = (request) => respond(request);

const ENV_KEYS = [
	'ORIGIN',
	'PROTOCOL_HEADER',
	'HOST_HEADER',
	'PORT_HEADER',
	'ADDRESS_HEADER',
	'HOST',
	'PORT',
	'SOCKET_PATH',
	'LISTEN_PID',
	'LISTEN_FDS'
] as const;

afterEach(() => {
	vi.unstubAllGlobals();
	respond.mockReset();
});

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

function parseCookies(cookieHeader: string | null): Map<string, string> {
	const map = new Map<string, string>();
	if (!cookieHeader) return map;
	for (const part of cookieHeader.split(';')) {
		const eq = part.indexOf('=');
		if (eq === -1) continue;
		map.set(part.slice(0, eq).trim(), decodeURIComponent(part.slice(eq + 1).trim()));
	}
	return map;
}

async function runThroughHooks(
	request: Request,
	resolveRoute: (event: { url: URL; request: Request }) => Promise<Response> = async (event) =>
		new Response(
			JSON.stringify({
				url: event.url.toString(),
				forwardedProto: event.request.headers.get('x-forwarded-proto'),
				forwardedHost: event.request.headers.get('x-forwarded-host'),
				forwardedFor: event.request.headers.get('x-forwarded-for')
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		)
): Promise<Response> {
	const incomingCookies = parseCookies(request.headers.get('cookie'));
	const setCookies: string[] = [];
	const response = await handle({
		event: {
			url: new URL(request.url),
			request,
			locals: {},
			cookies: {
				get: (name: string) => incomingCookies.get(name),
				set: (name: string, value: string, opts: Record<string, unknown>) => {
					setCookies.push(serializeCookie(name, value, opts));
				},
				delete: (name: string, opts: Record<string, unknown>) => {
					setCookies.push(serializeCookie(name, '', { ...opts, maxAge: 0 }));
				}
			}
		} as never,
		resolve: (event: never) => resolveRoute(event)
	} as never);
	const headers = new Headers(response.headers);
	for (const cookie of setCookies) {
		headers.append('set-cookie', cookie);
	}
	return new Response(response.body, { status: response.status, headers });
}

async function startRealAdapterServer(overrides: Record<string, string | undefined>) {
	const savedEnv = new Map<string, string | undefined>();
	for (const key of ENV_KEYS) {
		savedEnv.set(key, process.env[key]);
		delete process.env[key];
	}
	process.env.HOST = '127.0.0.1';
	process.env.PORT = '0';
	for (const [key, value] of Object.entries(overrides)) {
		if (value === undefined) delete process.env[key];
		else process.env[key] = value;
	}

	const sigtermBefore = new Set(process.listeners('SIGTERM'));
	const sigintBefore = new Set(process.listeners('SIGINT'));
	const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

	adapterRunId += 1;
	const adapterIndexUrl = `${new URL(
		'../node_modules/@sveltejs/adapter-node/src/index.js',
		import.meta.url
	).href}?serverSpecRun=${adapterRunId}`;
	const started = await startServer({
		env: process.env,
		loadServer: () => import(/* @vite-ignore */ adapterIndexUrl)
	});

	if (!started.server.listening) {
		await once(started.server, 'listening');
	}

	onTestFinished(async () => {
		logSpy.mockRestore();
		if (started.server.listening) {
			started.server.closeAllConnections?.();
			await new Promise<void>((resolveClose) => started.server.close(() => resolveClose()));
		}
		for (const listener of process.listeners('SIGTERM')) {
			if (!sigtermBefore.has(listener)) process.off('SIGTERM', listener);
		}
		for (const listener of process.listeners('SIGINT')) {
			if (!sigintBefore.has(listener)) process.off('SIGINT', listener);
		}
		for (const [key, value] of savedEnv) {
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
	});

	const { port } = started.server.address() as AddressInfo;
	return { ...started, port };
}

function sendHttpRequest(
	port: number,
	options: {
		method?: string;
		path?: string;
		headers?: Record<string, string>;
		body?: string;
	}
): Promise<{ status: number; headers: http.IncomingHttpHeaders; body: string }> {
	return new Promise((resolveResponse, rejectResponse) => {
		const bodyBuffer = options.body ? Buffer.from(options.body, 'utf8') : undefined;
		const req = http.request(
			{
				host: '127.0.0.1',
				port,
				method: options.method ?? 'GET',
				path: options.path ?? '/',
				headers: {
					...(bodyBuffer ? { 'content-length': String(bodyBuffer.byteLength) } : {}),
					...options.headers
				}
			},
			(res) => {
				const chunks: Buffer[] = [];
				res.on('data', (chunk: Buffer) => chunks.push(chunk));
				res.on('end', () => {
					resolveResponse({
						status: res.statusCode ?? 0,
						headers: res.headers,
						body: Buffer.concat(chunks).toString('utf8')
					});
				});
			}
		);
		req.on('error', rejectResponse);
		if (bodyBuffer) req.write(bodyBuffer);
		req.end();
	});
}

function mockEngineSession(profileId = 'alice-profile') {
	vi.stubGlobal(
		'fetch',
		vi.fn().mockImplementation(
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
		)
	);
}

describe('Node runtime origin wrapper', () => {
	it('deletes blank ORIGIN and proxy headers while leaving PROTOCOL_HEADER unset for https fallback', () => {
		const env: NodeJS.ProcessEnv = {
			ORIGIN: '   ',
			PROTOCOL_HEADER: '',
			HOST_HEADER: '  ',
			PORT_HEADER: '',
			ADDRESS_HEADER: ''
		};

		const config = prepareRuntimeEnv(env);

		expect(env.ORIGIN).toBeUndefined();
		expect(env.PROTOCOL_HEADER).toBeUndefined();
		expect(env.HOST_HEADER).toBeUndefined();
		expect(env.PORT_HEADER).toBeUndefined();
		expect(env.ADDRESS_HEADER).toBeUndefined();
		expect(config).toEqual({
			origin: undefined,
			protocolHeader: undefined,
			hostHeader: undefined,
			portHeader: undefined,
			addressHeader: undefined
		});
	});

	it('defaults to https://<Host> behind a TLS-terminating proxy with neither ORIGIN nor PROTOCOL_HEADER set', async () => {
		mockEngineSession();
		respond.mockImplementation((request) => runThroughHooks(request));

		const { port } = await startRealAdapterServer({
			ORIGIN: '',
			PROTOCOL_HEADER: '',
			HOST_HEADER: ''
		});

		const allowedPost = await sendHttpRequest(port, {
			method: 'POST',
			path: '/api/telemetry',
			headers: {
				host: 'books.example.com',
				origin: 'https://books.example.com',
				cookie: 'bookward_session=valid-session',
				'content-type': 'application/json',
				'x-forwarded-proto': 'http',
				'x-forwarded-host': 'attacker.example',
				'x-forwarded-for': '203.0.113.9'
			},
			body: JSON.stringify({ expected_profile_id: 'alice-profile', events: [] })
		});

		expect(allowedPost.status).toBe(200);
		expect(JSON.parse(allowedPost.body)).toEqual({
			url: 'https://books.example.com/api/telemetry',
			forwardedProto: null,
			forwardedHost: null,
			forwardedFor: null
		});
		const setCookie = [allowedPost.headers['set-cookie'] ?? []].flat().join('; ');
		expect(setCookie).toContain('bookward_session=valid-session');
		expect(setCookie).toContain('Secure');

		const downgradedOriginPost = await sendHttpRequest(port, {
			method: 'POST',
			path: '/api/telemetry',
			headers: {
				host: 'books.example.com',
				origin: 'http://books.example.com',
				cookie: 'bookward_session=valid-session',
				'content-type': 'application/json'
			},
			body: JSON.stringify({ expected_profile_id: 'alice-profile', events: [] })
		});
		expect(downgradedOriginPost.status).toBe(403);
	});

	it('honors explicit plain-HTTP runtime ORIGIN, strips untrusted headers, and delegates graceful shutdown to adapter-node', async () => {
		mockEngineSession();
		respond.mockImplementation((request) => runThroughHooks(request));

		const { port } = await startRealAdapterServer({
			ORIGIN: ' http://books.lan:3000 '
		});

		const res = await sendHttpRequest(port, {
			method: 'POST',
			path: '/api/telemetry',
			headers: {
				host: 'books.lan:3000',
				origin: 'http://books.lan:3000',
				cookie: 'bookward_session=valid-session',
				'content-type': 'application/json',
				'x-forwarded-proto': 'https',
				'x-forwarded-host': 'attacker.example'
			},
			body: JSON.stringify({ expected_profile_id: 'alice-profile', events: [] })
		});

		expect(res.status).toBe(200);
		expect(JSON.parse(res.body)).toEqual({
			url: 'http://books.lan:3000/api/telemetry',
			forwardedProto: null,
			forwardedHost: null,
			forwardedFor: null
		});
		const setCookie = [res.headers['set-cookie'] ?? []].flat().join('; ');
		expect(setCookie).toContain('bookward_session=valid-session');
		expect(setCookie).not.toContain('Secure');

		const shutdownReason = new Promise<string>((resolveShutdown) => {
			process.once('sveltekit:shutdown' as never, (reason: string) => resolveShutdown(reason));
		});
		process.emit('SIGTERM', 'SIGTERM');
		expect(await shutdownReason).toBe('SIGTERM');
	});

	it('preserves trusted reverse-proxy headers and strips non-configured forwarded headers', async () => {
		respond.mockImplementation((request) => runThroughHooks(request));

		const { port } = await startRealAdapterServer({
			ORIGIN: '',
			PROTOCOL_HEADER: ' X-Forwarded-Proto ',
			HOST_HEADER: ' X-Forwarded-Host '
		});

		const res = await sendHttpRequest(port, {
			method: 'GET',
			path: '/login',
			headers: {
				host: '127.0.0.1',
				'x-forwarded-proto': 'https',
				'x-forwarded-host': 'books.example.com',
				'x-forwarded-for': '203.0.113.9'
			}
		});

		expect(res.status).toBe(200);
		expect(JSON.parse(res.body)).toEqual({
			url: 'https://books.example.com/login',
			forwardedProto: 'https',
			forwardedHost: 'books.example.com',
			forwardedFor: null
		});

		const fallbackRes: { statusCode?: number; ended?: boolean } = {};
		const wrapped = wrapAdapterHandler((_req, _res, next) => next(), prepareRuntimeEnv({}));
		wrapped(
			{ headers: { 'x-forwarded-proto': 'http' } } as never,
			{
				set statusCode(code: number) {
					fallbackRes.statusCode = code;
				},
				end: () => {
					fallbackRes.ended = true;
				}
			} as never
		);
		expect(fallbackRes).toEqual({ statusCode: 404, ended: true });
	});
});
