import { EventEmitter } from 'node:events';
import type { AddressInfo } from 'node:net';
import { describe, expect, it, vi } from 'vitest';
import {
	SOCKET_PROTOCOL_HEADER,
	createRequestListener,
	parseTimeoutEnv,
	prepareRuntimeEnv,
	socketProtocol,
	startServer
} from '../server.js';

describe('Node runtime origin wrapper', () => {
	it('deletes blank ORIGIN and proxy headers before handler initialization', () => {
		const env: NodeJS.ProcessEnv = {
			ORIGIN: '   ',
			PROTOCOL_HEADER: '',
			HOST_HEADER: '  ',
			PORT_HEADER: '',
			ADDRESS_HEADER: ''
		};

		const config = prepareRuntimeEnv(env);

		expect(env.ORIGIN).toBeUndefined();
		expect(env.HOST_HEADER).toBeUndefined();
		expect(env.PORT_HEADER).toBeUndefined();
		expect(env.ADDRESS_HEADER).toBeUndefined();
		expect(env.PROTOCOL_HEADER).toBe(SOCKET_PROTOCOL_HEADER);
		expect(config).toEqual({
			origin: undefined,
			protocolHeader: undefined,
			hostHeader: undefined,
			portHeader: undefined,
			addressHeader: undefined,
			useSocketProtocol: true
		});
	});

	it('honors configured runtime ORIGIN and trusted proxy headers', () => {
		const withOrigin: NodeJS.ProcessEnv = {
			ORIGIN: ' https://books.example.com '
		};
		const originConfig = prepareRuntimeEnv(withOrigin);
		expect(withOrigin.ORIGIN).toBe('https://books.example.com');
		expect(withOrigin.PROTOCOL_HEADER).toBeUndefined();
		expect(originConfig.useSocketProtocol).toBe(false);

		const withProxy: NodeJS.ProcessEnv = {
			ORIGIN: '',
			PROTOCOL_HEADER: ' X-Forwarded-Proto ',
			HOST_HEADER: ' X-Forwarded-Host '
		};
		const proxyConfig = prepareRuntimeEnv(withProxy);
		expect(withProxy.ORIGIN).toBeUndefined();
		expect(withProxy.PROTOCOL_HEADER).toBe('x-forwarded-proto');
		expect(withProxy.HOST_HEADER).toBe('x-forwarded-host');
		expect(proxyConfig).toMatchObject({
			origin: undefined,
			protocolHeader: 'x-forwarded-proto',
			hostHeader: 'x-forwarded-host',
			useSocketProtocol: false
		});
	});

	it('ignores untrusted X-Forwarded-* headers and injects the socket scheme when unset', async () => {
		const env: NodeJS.ProcessEnv = {
			ORIGIN: '',
			HOST: '127.0.0.1',
			PORT: '0',
			KEEP_ALIVE_TIMEOUT: '9',
			HEADERS_TIMEOUT: '12'
		};
		const fakeProcess = Object.assign(new EventEmitter(), { pid: 4242 });
		let seenHeaders: Record<string, unknown> = {};

		const started = await startServer({
			env,
			processTarget: fakeProcess,
			log: vi.fn(),
			loadHandler: async () => ({
				handler: (req, res, next) => {
					seenHeaders = { ...req.headers };
					if (req.url === '/missing') {
						next();
						return;
					}
					res.writeHead(200, { 'content-type': 'application/json' });
					res.end(JSON.stringify({ proto: req.headers[SOCKET_PROTOCOL_HEADER], host: req.headers.host }));
				}
			})
		});

		expect(started.server.keepAliveTimeout).toBe(9000);
		expect(started.server.headersTimeout).toBe(12000);

		const { port } = started.server.address() as AddressInfo;
		const response = await fetch(`http://127.0.0.1:${port}/login`, {
			headers: {
				'x-forwarded-proto': 'https',
				'x-forwarded-host': 'attacker.example',
				'x-forwarded-for': '203.0.113.9',
				[SOCKET_PROTOCOL_HEADER]: 'https'
			}
		});

		expect(response.status).toBe(200);
		expect(await response.json()).toEqual({
			proto: 'http',
			host: `127.0.0.1:${port}`
		});
		expect(seenHeaders['x-forwarded-proto']).toBeUndefined();
		expect(seenHeaders['x-forwarded-host']).toBeUndefined();
		expect(seenHeaders['x-forwarded-for']).toBeUndefined();

		const notFound = await fetch(`http://127.0.0.1:${port}/missing`);
		expect(notFound.status).toBe(404);

		const shutdownEvent = new Promise<string>((resolveShutdown) => {
			fakeProcess.once('sveltekit:shutdown', (reason: string) => resolveShutdown(reason));
		});
		fakeProcess.emit('SIGTERM');
		expect(await shutdownEvent).toBe('SIGTERM');
	});

	it('preserves trusted proxy headers and TLS socket detection, and shuts down cleanly on SIGINT', async () => {
		expect(socketProtocol({ encrypted: true } as never)).toBe('https');
		expect(socketProtocol({} as never)).toBe('http');

		const tlsConfig = prepareRuntimeEnv({});
		let tlsReceivedProto: unknown;
		const tlsListener = createRequestListener(
			(req, res) => {
				tlsReceivedProto = req.headers[SOCKET_PROTOCOL_HEADER];
				res.end();
			},
			tlsConfig
		);
		tlsListener(
			{
				headers: { 'x-forwarded-proto': 'http' },
				socket: { encrypted: true }
			} as never,
			{ end: vi.fn() } as never
		);
		expect(tlsReceivedProto).toBe('https');

		const proxyEnv: NodeJS.ProcessEnv = {
			HOST: '127.0.0.1',
			PORT: '0',
			PROTOCOL_HEADER: 'x-forwarded-proto',
			HOST_HEADER: 'x-forwarded-host'
		};
		const fakeProcess = Object.assign(new EventEmitter(), { pid: 5151 });
		const started = await startServer({
			env: proxyEnv,
			processTarget: fakeProcess,
			log: vi.fn(),
			handler: (req, res) => {
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(
					JSON.stringify({
						forwardedProto: req.headers['x-forwarded-proto'],
						forwardedHost: req.headers['x-forwarded-host'],
						socketHeader: req.headers[SOCKET_PROTOCOL_HEADER] ?? null
					})
				);
			}
		});

		const { port } = started.server.address() as AddressInfo;
		const res = await fetch(`http://127.0.0.1:${port}/`, {
			headers: {
				'x-forwarded-proto': 'https',
				'x-forwarded-host': 'books.example.com',
				[SOCKET_PROTOCOL_HEADER]: 'http'
			}
		});
		expect(await res.json()).toEqual({
			forwardedProto: 'https',
			forwardedHost: 'books.example.com',
			socketHeader: null
		});

		const shutdownEvent = new Promise<string>((resolveShutdown) => {
			fakeProcess.once('sveltekit:shutdown', (reason: string) => resolveShutdown(reason));
		});
		fakeProcess.emit('SIGINT');
		expect(await shutdownEvent).toBe('SIGINT');

		expect(() => parseTimeoutEnv({ KEEP_ALIVE_TIMEOUT: '-5' }, 'KEEP_ALIVE_TIMEOUT')).toThrow(
			/non-negative integer/
		);
	});
});
