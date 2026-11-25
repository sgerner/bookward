import { AsyncLocalStorage } from 'node:async_hooks';

export type RequestContext = { sessionToken?: string; clientIp?: string };
const requestContext = new AsyncLocalStorage<RequestContext>();

type SocketPlatform = {
	req?: {
		socket?: { remoteAddress?: string };
		connection?: { remoteAddress?: string; socket?: { remoteAddress?: string } };
	};
};

let warnedAddressHeaderFallback = false;

export function resetClientAddressFallbackWarning(): void {
	warnedAddressHeaderFallback = false;
}

function warnMissingAddressHeaderOnce(): void {
	if (warnedAddressHeaderFallback) return;
	warnedAddressHeaderFallback = true;
	console.warn(
		'[bookward] Configured ADDRESS_HEADER could not resolve a client IP on an incoming request; falling back to socket remoteAddress (further occurrences suppressed).'
	);
}

export function resolveClientAddress(event: {
	getClientAddress?: () => string;
	platform?: unknown;
}): string | undefined {
	if (typeof event.getClientAddress === 'function') {
		try {
			const address = event.getClientAddress()?.trim();
			if (address) return address;
			if (process.env.ADDRESS_HEADER?.trim()) {
				warnMissingAddressHeaderOnce();
			}
		} catch (error) {
			// Fall back to the underlying socket address when a configured proxy header
			// is absent on a local request (such as a health check or direct connection),
			// logging a single warning so proxy misconfigurations do not fail silently.
			if (
				process.env.ADDRESS_HEADER?.trim() ||
				(error instanceof Error && /ADDRESS_HEADER|XFF_DEPTH/i.test(error.message))
			) {
				warnMissingAddressHeaderOnce();
			}
		}
	}
	const req = (event.platform as SocketPlatform | undefined)?.req;
	const fallback =
		req?.socket?.remoteAddress ||
		req?.connection?.remoteAddress ||
		req?.connection?.socket?.remoteAddress;
	return fallback?.trim() || undefined;
}

export function withRequestContext<T>(context: RequestContext, callback: () => T): T {
	const parent = requestContext.getStore();
	return requestContext.run({ ...parent, ...context }, callback);
}

export function withSessionToken<T>(
	sessionToken: string | undefined,
	callback: () => T,
	clientIp?: string
): T {
	const parent = requestContext.getStore();
	return requestContext.run(
		{
			sessionToken,
			clientIp: clientIp !== undefined ? clientIp : parent?.clientIp
		},
		callback
	);
}

export function currentSessionToken(): string | undefined {
	return requestContext.getStore()?.sessionToken;
}

export function setCurrentSessionToken(sessionToken: string | undefined): void {
	const store = requestContext.getStore();
	if (store) {
		store.sessionToken = sessionToken;
	}
}

export function currentClientIp(): string | undefined {
	return requestContext.getStore()?.clientIp;
}
