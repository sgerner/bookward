import { AsyncLocalStorage } from 'node:async_hooks';

type RequestContext = { sessionToken?: string };
const requestContext = new AsyncLocalStorage<RequestContext>();

export function withSessionToken<T>(sessionToken: string | undefined, callback: () => T): T {
	return requestContext.run({ sessionToken }, callback);
}

export function currentSessionToken(): string | undefined {
	return requestContext.getStore()?.sessionToken;
}
