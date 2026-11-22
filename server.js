import fs from 'node:fs';
import { rm } from 'node:fs/promises';
import http from 'node:http';
import { resolve } from 'node:path';
import process from 'node:process';
import { fileURLToPath, format } from 'node:url';

/**
 * @typedef {(
 *   req: import('node:http').IncomingMessage,
 *   res: import('node:http').ServerResponse,
 *   next: () => void
 * ) => unknown} AdapterHandler
 */

/**
 * @typedef {{
 *   origin: string | undefined;
 *   protocolHeader: string | undefined;
 *   hostHeader: string | undefined;
 *   portHeader: string | undefined;
 *   addressHeader: string | undefined;
 *   useSocketProtocol: boolean;
 * }} RuntimeOriginConfig
 */

export const SOCKET_PROTOCOL_HEADER = 'x-bookward-socket-proto';

const INTEGER_REGEXP = /^\d+$/;

/**
 * Trims an environment variable and deletes it when blank so empty `.env` or Compose
 * entries behave identically to unset variables.
 *
 * @param {NodeJS.ProcessEnv} env
 * @param {string} name
 * @returns {string | undefined}
 */
function trimOrDelete(env, name) {
	const raw = env[name];
	if (raw === undefined) return undefined;
	const trimmed = raw.trim();
	if (!trimmed) {
		delete env[name];
		return undefined;
	}
	env[name] = trimmed;
	return trimmed;
}

/**
 * Normalizes runtime origin and proxy-header environment variables before
 * `build/handler.js` evaluates `process.env`.
 *
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {RuntimeOriginConfig}
 */
export function prepareRuntimeEnv(env = process.env) {
	const origin = trimOrDelete(env, 'ORIGIN');
	const rawProtocolHeader = trimOrDelete(env, 'PROTOCOL_HEADER')?.toLowerCase();
	const protocolHeader =
		rawProtocolHeader && rawProtocolHeader !== SOCKET_PROTOCOL_HEADER
			? rawProtocolHeader
			: undefined;
	const hostHeader = trimOrDelete(env, 'HOST_HEADER')?.toLowerCase();
	const portHeader = trimOrDelete(env, 'PORT_HEADER')?.toLowerCase();
	const addressHeader = trimOrDelete(env, 'ADDRESS_HEADER')?.toLowerCase();

	if (protocolHeader) {
		env.PROTOCOL_HEADER = protocolHeader;
	} else if (!origin) {
		env.PROTOCOL_HEADER = SOCKET_PROTOCOL_HEADER;
	} else {
		delete env.PROTOCOL_HEADER;
	}

	if (hostHeader) env.HOST_HEADER = hostHeader;
	if (portHeader) env.PORT_HEADER = portHeader;
	if (addressHeader) env.ADDRESS_HEADER = addressHeader;

	return {
		origin,
		protocolHeader,
		hostHeader,
		portHeader,
		addressHeader,
		useSocketProtocol: !origin && !protocolHeader
	};
}

/**
 * Determines whether the underlying socket uses TLS.
 *
 * @param {import('node:http').IncomingMessage['socket'] | undefined} socket
 * @returns {'https' | 'http'}
 */
export function socketProtocol(socket) {
	return socket && 'encrypted' in socket && Boolean(socket.encrypted) ? 'https' : 'http';
}

/**
 * Wraps the SvelteKit `build/handler.js` middleware to enforce proxy-header trust
 * boundaries and propagate the socket scheme when `ORIGIN` and `PROTOCOL_HEADER` are unset.
 *
 * @param {AdapterHandler} handler
 * @param {RuntimeOriginConfig} config
 * @returns {(req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse) => unknown}
 */
export function createRequestListener(handler, config) {
	const trustedHeaders = new Set(
		[config.protocolHeader, config.hostHeader, config.portHeader, config.addressHeader].filter(
			/** @param {string | undefined} value @returns {value is string} */
			(value) => Boolean(value)
		)
	);

	return (req, res) => {
		delete req.headers[SOCKET_PROTOCOL_HEADER];

		for (const header of [
			'x-forwarded-proto',
			'x-forwarded-host',
			'x-forwarded-port',
			'x-forwarded-for',
			'forwarded'
		]) {
			if (!trustedHeaders.has(header)) {
				delete req.headers[header];
			}
		}

		if (config.useSocketProtocol) {
			req.headers[SOCKET_PROTOCOL_HEADER] = socketProtocol(req.socket);
		}

		return handler(req, res, () => {
			res.statusCode = 404;
			res.end();
		});
	};
}

/**
 * Parses a non-negative integer timeout environment variable (in seconds).
 *
 * @param {NodeJS.ProcessEnv} env
 * @param {string} name
 * @param {number} [fallback]
 * @returns {number | undefined}
 */
export function parseTimeoutEnv(env, name, fallback) {
	const raw = env[name];
	if (raw === undefined || raw === '') return fallback;
	if (!INTEGER_REGEXP.test(raw)) {
		throw new Error(
			`Invalid value for environment variable ${name}: ${JSON.stringify(raw)} (should be a non-negative integer)`
		);
	}
	return Number.parseInt(raw, 10);
}

/**
 * Formats the listening address for startup logs.
 *
 * @param {string | false} socketPath
 * @param {string} host
 * @param {string | false} port
 * @param {import('node:net').AddressInfo | string | null} address
 * @returns {string}
 */
function formatListeningAddress(socketPath, host, port, address) {
	if (socketPath) return socketPath;
	if (address && typeof address === 'object') {
		return format({
			protocol: 'http:',
			hostname: address.address,
			port: address.port
		});
	}
	return format({
		protocol: 'http:',
		hostname: host,
		port: String(port)
	});
}

/**
 * @typedef {{
 *   pid: number;
 *   on: (event: string, listener: (...args: any[]) => void) => unknown;
 *   off: (event: string, listener: (...args: any[]) => void) => unknown;
 *   emit: (event: string, ...args: any[]) => boolean;
 * }} ProcessTarget
 */

/**
 * Starts the Node HTTP server around `build/handler.js`.
 *
 * @param {{
 *   env?: NodeJS.ProcessEnv;
 *   handler?: AdapterHandler;
 *   loadHandler?: () => Promise<{ handler: AdapterHandler }>;
 *   processTarget?: ProcessTarget;
 *   log?: (message: string) => void;
 * }} [options]
 */
export async function startServer(options = {}) {
	const env = options.env ?? process.env;
	/** @type {ProcessTarget} */
	const proc =
		options.processTarget ?? /** @type {ProcessTarget} */ (/** @type {unknown} */ (process));
	const log = options.log ?? console.log;

	const originConfig = prepareRuntimeEnv(env);
	const handler =
		options.handler ??
		(
			await (options.loadHandler ??
				(() => import(new URL('./build/handler.js', import.meta.url).href)))()
		).handler;

	const socketPath = env.SOCKET_PATH || false;
	const host = env.HOST ?? '0.0.0.0';
	const port = env.PORT ?? (!socketPath ? '3000' : false);

	const shutdownTimeout = parseTimeoutEnv(env, 'SHUTDOWN_TIMEOUT', 30) ?? 30;
	const idleTimeout = parseTimeoutEnv(env, 'IDLE_TIMEOUT', 0) ?? 0;
	const listenPid = Number.parseInt(env.LISTEN_PID ?? '0', 10);
	const listenFds = Number.parseInt(env.LISTEN_FDS ?? '0', 10);
	const sdListenFdsStart = 3;

	if (listenPid !== 0 && listenPid !== proc.pid) {
		throw new Error(`received LISTEN_PID ${listenPid} but current process id is ${proc.pid}`);
	}
	if (listenFds > 1) {
		throw new Error(
			`only one socket is allowed for socket activation, but LISTEN_FDS was set to ${listenFds}`
		);
	}

	const socketActivation = listenPid === proc.pid && listenFds === 1;
	const requestListener = createRequestListener(handler, originConfig);

	let activeRequests = 0;
	/** @type {ReturnType<typeof setTimeout> | undefined} */
	let shutdownTimeoutId;
	/** @type {ReturnType<typeof setTimeout> | undefined} */
	let idleTimeoutId;

	const server = http.createServer();

	const keepAliveTimeout = parseTimeoutEnv(env, 'KEEP_ALIVE_TIMEOUT');
	if (keepAliveTimeout !== undefined) {
		server.keepAliveTimeout = keepAliveTimeout * 1000;
	}

	const headersTimeout = parseTimeoutEnv(env, 'HEADERS_TIMEOUT');
	if (headersTimeout !== undefined) {
		server.headersTimeout = headersTimeout * 1000;
	}

	/** @param {'SIGINT' | 'SIGTERM' | 'IDLE'} reason */
	function gracefulShutdown(reason) {
		if (shutdownTimeoutId) return;

		server.closeIdleConnections();

		server.close((error) => {
			if (error) return;

			if (shutdownTimeoutId) clearTimeout(shutdownTimeoutId);
			if (idleTimeoutId) clearTimeout(idleTimeoutId);
			proc.off('SIGTERM', onSigterm);
			proc.off('SIGINT', onSigint);

			proc.emit('sveltekit:shutdown', reason);
		});

		shutdownTimeoutId = setTimeout(
			() => server.closeAllConnections(),
			shutdownTimeout * 1000
		);
		shutdownTimeoutId.unref?.();
	}

	const onSigterm = () => gracefulShutdown('SIGTERM');
	const onSigint = () => gracefulShutdown('SIGINT');

	server.on('request', (req, res) => {
		activeRequests++;

		if (socketActivation && idleTimeoutId) {
			clearTimeout(idleTimeoutId);
			idleTimeoutId = undefined;
		}

		req.on('close', () => {
			activeRequests--;

			if (shutdownTimeoutId) {
				server.closeIdleConnections();
			}
			if (activeRequests === 0 && socketActivation && idleTimeout) {
				idleTimeoutId = setTimeout(() => gracefulShutdown('IDLE'), idleTimeout * 1000);
			}
		});

		return requestListener(req, res);
	});

	if (!socketActivation && socketPath) {
		try {
			if (fs.statSync(socketPath).size === 0) {
				await rm(socketPath);
			}
		} catch {
			// Ignore missing socket path prior to bind.
		}
	}

	await new Promise((resolveListen, rejectListen) => {
		server.once('error', rejectListen);
		if (socketActivation) {
			server.listen({ fd: sdListenFdsStart }, () => {
				server.off('error', rejectListen);
				log(`Listening on file descriptor ${sdListenFdsStart}`);
				resolveListen(undefined);
			});
		} else {
			server.listen(
				socketPath ? { path: socketPath } : { host, port: Number(port) },
				() => {
					server.off('error', rejectListen);
					log(
						`Listening on ${formatListeningAddress(socketPath, host, port, server.address())}`
					);
					resolveListen(undefined);
				}
			);
		}
	});

	proc.on('SIGTERM', onSigterm);
	proc.on('SIGINT', onSigint);

	return {
		server,
		originConfig,
		gracefulShutdown
	};
}

const isMain =
	Boolean(process.argv[1]) && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
	await startServer();
}
