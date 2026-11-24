import { AsyncLocalStorage } from 'node:async_hooks';

type RequestContext = { sessionToken?: string };
const requestContext = new AsyncLocalStorage<RequestContext>();
const formDataByRequest = new WeakMap<Request, Promise<FormData>>();

export function withSessionToken<T>(sessionToken: string | undefined, callback: () => T): T {
	return requestContext.run({ sessionToken }, callback);
}

export function currentSessionToken(): string | undefined {
	return requestContext.getStore()?.sessionToken;
}

export function requestFormData(request: Request): Promise<FormData> {
	let cached = formDataByRequest.get(request);
	if (!cached) {
		const parse = request.formData.bind(request);
		cached = parse();
		formDataByRequest.set(request, cached);
		Object.defineProperty(request, 'formData', {
			configurable: true,
			value: () => cached!
		});
	}
	return cached;
}

export function hasCachedFormData(request: Request): boolean {
	return formDataByRequest.has(request);
}
