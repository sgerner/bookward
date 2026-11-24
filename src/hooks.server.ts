import type { Handle } from '@sveltejs/kit/hooks';
import { error as httpError, redirect } from '@sveltejs/kit';
import {
	EngineError,
	GOODREADS_CSV_LIMIT_MESSAGE,
	engine,
	isPayloadTooLarge
} from '#lib/server/engine.js';
import {
	SESSION_COOKIE,
	sessionCookieDeleteOptions,
	sessionRefreshCookieOptions
} from '#lib/server/cookies.js';
import { withSessionToken } from '#lib/server/request-context.js';

const formDataByRequest = new WeakMap<Request, Promise<FormData>>();

function requestFormData(request: Request): Promise<FormData> {
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

function jsonError(status: number, detail: string) {
	return withSecurityHeaders(new Response(JSON.stringify({ detail, message: detail }), {
		status,
		headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }
	}));
}

function prefersHtmlError(request: Request, path: string): boolean {
	if (path.startsWith('/api/') || request.headers.get('x-sveltekit-action') === 'true') return false;
	const accept = (request.headers.get('accept') || '').toLowerCase();
	const htmlIndex = accept.indexOf('text/html');
	if (htmlIndex === -1) return false;
	const jsonIndex = accept.indexOf('application/json');
	return jsonIndex === -1 || htmlIndex < jsonIndex;
}

export function isPublicApiPath(pathname: string) {
	let decoded: string;
	try {
		decoded = decodeURIComponent(pathname).replaceAll('\\', '/');
	} catch {
		return false;
	}
	if (decoded.split('/').some((segment) => segment === '.' || segment === '..')) return false;
	return decoded === '/api/v1' || decoded.startsWith('/api/v1/');
}

export function withSecurityHeaders(response: Response) {
	response.headers.set('x-content-type-options', 'nosniff');
	response.headers.set('x-frame-options', 'DENY');
	response.headers.set('referrer-policy', 'strict-origin-when-cross-origin');
	response.headers.set('permissions-policy', 'camera=(), geolocation=(), microphone=()');
	return response;
}

function isAsset(pathname: string) {
	return pathname.startsWith('/_app/') || pathname === '/favicon.ico' || pathname === '/favicon.svg' ||
		pathname.startsWith('/favicon-') || pathname.startsWith('/apple-touch-icon') ||
		pathname.startsWith('/android-chrome-') || pathname === '/site.webmanifest' || pathname === '/robots.txt';
}

async function sessionUser(token: string) {
	try {
		return await engine<NonNullable<App.Locals['user']>>('/auth/me', {
			headers: { 'x-bookward-session': token }
		});
	} catch (error) {
		if (error instanceof EngineError && error.status === 401) return null;
		throw error;
	}
}

async function submittedProfile(request: Request): Promise<string> {
	const header = request.headers.get('x-bookward-profile');
	if (header) return header;
	const requestUrl = new URL(request.url);
	if (requestUrl.pathname === '/auth/oidc/start') return requestUrl.searchParams.get('profile_id') || '';
	const contentType = request.headers.get('content-type') || '';
	try {
		if (contentType.includes('multipart/form-data') || contentType.includes('application/x-www-form-urlencoded')) {
			const form = await requestFormData(request);
			return String(form.get('__profile_id') || '');
		}
		if (contentType.includes('application/json')) {
			const body = await request.clone().json() as { expected_profile_id?: unknown };
			return typeof body.expected_profile_id === 'string' ? body.expected_profile_id : '';
		}
	} catch (cause) {
		if (isPayloadTooLarge(cause)) throw cause;
		return '';
	}
	return '';
}

export const handle: Handle = async ({ event, resolve }) => {
	const path = event.url.pathname;
	const session = event.cookies.get(SESSION_COOKIE);

	if (isPublicApiPath(path) || isAsset(path)) return withSecurityHeaders(await resolve(event));
	const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(event.request.method);
	if (mutation) {
		const origin = event.request.headers.get('origin');
		const referrer = event.request.headers.get('referer');
		const fetchSite = event.request.headers.get('sec-fetch-site');
		let sameOriginReferrer = false;
		try { sameOriginReferrer = Boolean(referrer) && new URL(referrer!).origin === event.url.origin; } catch { /* Reject malformed referrers. */ }
		if ((origin && origin !== event.url.origin) ||
			(!origin && !sameOriginReferrer && fetchSite !== 'same-origin')) {
			return jsonError(403, 'Request origin could not be verified');
		}
	}
	const oidcIntent = path === '/auth/oidc/start' ? event.url.searchParams.get('intent') : null;
	const sessionBoundOidcStart = oidcIntent === 'link' || oidcIntent === 'enroll';
	if (session && sessionBoundOidcStart) {
		const origin = event.request.headers.get('origin');
		const fetchSite = event.request.headers.get('sec-fetch-site');
		if ((origin && origin !== event.url.origin) || (!origin && fetchSite !== 'same-origin')) {
			return withSecurityHeaders(new Response('Request origin could not be verified', { status: 403 }));
		}
	}

	if (path === '/auth/oidc/start' && oidcIntent === 'login') {
		return withSecurityHeaders(await resolve(event));
	}

	const callback = path === '/auth/oidc/callback';
	if (path === '/login' && session) {
		const user = await sessionUser(session);
		if (user) throw redirect(303, '/');
		event.cookies.delete(SESSION_COOKIE, sessionCookieDeleteOptions(event.url));
		return withSecurityHeaders(await resolve(event));
	}

	if (!session && path !== '/login' && !callback) {
		if (path.startsWith('/api/')) return withSecurityHeaders(new Response(JSON.stringify({ detail: 'Authentication required' }), { status: 401, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } }));
		const next = `${event.url.pathname}${event.url.search}`;
		throw redirect(303, `/login?next=${encodeURIComponent(next)}`);
	}

	if (!session) return withSecurityHeaders(await resolve(event));

	const user = await sessionUser(session);
	if (!user) {
		event.cookies.delete(SESSION_COOKIE, sessionCookieDeleteOptions(event.url));
		if (callback) return withSecurityHeaders(await resolve(event));
		if (path.startsWith('/api/')) return withSecurityHeaders(new Response(JSON.stringify({ detail: 'Authentication required' }), { status: 401, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } }));
		throw redirect(303, `/login?next=${encodeURIComponent(`${event.url.pathname}${event.url.search}`)}`);
	}
	const boundProfile = event.request.headers.has('x-bookward-profile') || mutation || sessionBoundOidcStart;
	if (boundProfile) {
		let expected = '';
		try {
			expected = await submittedProfile(event.request);
		} catch (cause) {
			if (isPayloadTooLarge(cause)) {
				if (prefersHtmlError(event.request, path)) {
					throw httpError(413, GOODREADS_CSV_LIMIT_MESSAGE);
				}
				return jsonError(413, GOODREADS_CSV_LIMIT_MESSAGE);
			}
			throw cause;
		}
		if (!expected || expected !== user.profile_id) {
			return jsonError(409, 'This page belongs to a different profile. Reload before continuing.');
		}
	}
	event.locals.user = user;
	if (user.must_change_password && path !== '/account' && !path.startsWith('/auth/')) throw redirect(303, '/account?force_password_change=1');
	event.cookies.set(SESSION_COOKIE, session, sessionRefreshCookieOptions(event.url));
	const response = await withSessionToken(session, () => resolve(event));
	response.headers.set('cache-control', 'private, no-store');
	return withSecurityHeaders(response);
};
