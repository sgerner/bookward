import { resolve } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

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
 * }} RuntimeOriginConfig
 */

const FORWARDED_HEADERS = [
	'x-forwarded-proto',
	'x-forwarded-host',
	'x-forwarded-port',
	'x-forwarded-for',
	'forwarded'
];

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
 * `@sveltejs/adapter-node` evaluates `process.env`.
 *
 * When neither `ORIGIN` nor `PROTOCOL_HEADER` is configured, both remain unset so
 * `@sveltejs/adapter-node` preserves its default `https` scheme behind
 * TLS-terminating reverse proxies.
 *
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {RuntimeOriginConfig}
 */
export function prepareRuntimeEnv(env = process.env) {
	const origin = trimOrDelete(env, 'ORIGIN');
	const protocolHeader = trimOrDelete(env, 'PROTOCOL_HEADER')?.toLowerCase();
	const hostHeader = trimOrDelete(env, 'HOST_HEADER')?.toLowerCase();
	const portHeader = trimOrDelete(env, 'PORT_HEADER')?.toLowerCase();
	const addressHeader = trimOrDelete(env, 'ADDRESS_HEADER')?.toLowerCase();

	if (protocolHeader) env.PROTOCOL_HEADER = protocolHeader;
	if (hostHeader) env.HOST_HEADER = hostHeader;
	if (portHeader) env.PORT_HEADER = portHeader;
	if (addressHeader) env.ADDRESS_HEADER = addressHeader;

	return {
		origin,
		protocolHeader,
		hostHeader,
		portHeader,
		addressHeader
	};
}

/**
 * @param {RuntimeOriginConfig} config
 * @returns {Set<string>}
 */
function trustedForwardedHeaders(config) {
	return new Set(
		[config.protocolHeader, config.hostHeader, config.portHeader, config.addressHeader].filter(
			/** @param {string | undefined} value @returns {value is string} */
			(value) => Boolean(value)
		)
	);
}

/**
 * Strips untrusted `X-Forwarded-*` and `Forwarded` headers from `headers`.
 *
 * @param {import('node:http').IncomingHttpHeaders} headers
 * @param {RuntimeOriginConfig} config
 */
export function sanitizeForwardedHeaders(headers, config) {
	const trusted = trustedForwardedHeaders(config);
	for (const header of FORWARDED_HEADERS) {
		if (!trusted.has(header)) {
			delete headers[header];
		}
	}
}

/**
 * Wraps `@sveltejs/adapter-node`'s `build/handler.js` middleware to strip
 * untrusted forwarded headers before origin and client-address resolution.
 *
 * @param {AdapterHandler} handler
 * @param {RuntimeOriginConfig} config
 * @returns {(req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse, next?: () => void) => unknown}
 */
export function wrapAdapterHandler(handler, config) {
	const trusted = trustedForwardedHeaders(config);
	return (req, res, next) => {
		for (const header of FORWARDED_HEADERS) {
			if (!trusted.has(header)) {
				delete req.headers[header];
			}
		}
		return handler(
			req,
			res,
			next ??
				(() => {
					res.statusCode = 404;
					res.end();
				})
		);
	};
}

/**
 * Prepends forwarded-header sanitization onto `@sveltejs/adapter-node`'s
 * `http.Server` so all adapter server lifecycle logic (socket activation,
 * timeouts, graceful shutdown) runs unmodified.
 *
 * @template {import('node:http').Server} TServer
 * @param {TServer} server
 * @param {RuntimeOriginConfig} config
 * @returns {TServer}
 */
export function attachOriginGuard(server, config) {
	const trusted = trustedForwardedHeaders(config);
	server.prependListener(
		'request',
		/** @param {import('node:http').IncomingMessage} req */
		(req) => {
			for (const header of FORWARDED_HEADERS) {
				if (!trusted.has(header)) {
					delete req.headers[header];
				}
			}
		}
	);
	return server;
}

/**
 * Normalizes runtime environment variables and delegates HTTP server creation
 * and lifecycle management to `@sveltejs/adapter-node`'s `./build/index.js`.
 *
 * @param {{
 *   env?: NodeJS.ProcessEnv;
 *   loadServer?: () => Promise<{ server: import('node:http').Server }>;
 * }} [options]
 */
export async function startServer(options = {}) {
	const env = options.env ?? process.env;
	const originConfig = prepareRuntimeEnv(env);
	const loadServer =
		options.loadServer ?? (() => import(new URL('./build/index.js', import.meta.url).href));
	const { server } = await loadServer();
	attachOriginGuard(server, originConfig);
	return {
		server,
		originConfig
	};
}

const isMain =
	Boolean(process.argv[1]) && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
	await startServer();
}
