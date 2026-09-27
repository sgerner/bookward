import type { Handle } from '@sveltejs/kit';
import { redirect } from '@sveltejs/kit';
import { timingSafeEqual } from 'node:crypto';
import { EngineError, engine } from '$lib/server/engine';
import { withSessionToken } from '$lib/server/request-context';

const SESSION_COOKIE = 'bookward_session';

// Kept as a narrow utility for installations migrating from the former
// Basic-auth configuration. It is no longer used as the browser auth flow.
function equal(left: string, right: string) {
	const a = Buffer.from(left); const b = Buffer.from(right);
	return a.length === b.length && timingSafeEqual(a, b);
}

export function authorized(request: Request, username: string, password: string) {
	const header = request.headers.get('authorization') || '';
	if (!header.startsWith('Basic ')) return false;
	try {
		const decoded = Buffer.from(header.slice(6), 'base64').toString('utf8');
		const separator = decoded.indexOf(':');
		return separator >= 0 && equal(decoded.slice(0, separator), username) && equal(decoded.slice(separator + 1), password);
	} catch {
		return false;
	}
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
			const form = await request.clone().formData();
			return String(form.get('__profile_id') || '');
		}
		if (contentType.includes('application/json')) {
			const body = await request.clone().json() as { expected_profile_id?: unknown };
			return typeof body.expected_profile_id === 'string' ? body.expected_profile_id : '';
		}
	} catch {
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
			return withSecurityHeaders(new Response(JSON.stringify({ detail: 'Request origin could not be verified' }), {
				status: 403, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }
			}));
		}
	}
	if (session && path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'link') {
		const origin = event.request.headers.get('origin');
		const fetchSite = event.request.headers.get('sec-fetch-site');
		if ((origin && origin !== event.url.origin) || (!origin && fetchSite !== 'same-origin')) {
			return withSecurityHeaders(new Response('Request origin could not be verified', { status: 403 }));
		}
	}

	if (path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'login') {
		return withSecurityHeaders(await resolve(event));
	}

	const callback = path === '/auth/oidc/callback';
	if (path === '/login' && session) {
		const user = await sessionUser(session);
		if (user) throw redirect(303, '/');
		event.cookies.delete(SESSION_COOKIE, { path: '/' });
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
		event.cookies.delete(SESSION_COOKIE, { path: '/' });
		if (callback) return withSecurityHeaders(await resolve(event));
		if (path.startsWith('/api/')) return withSecurityHeaders(new Response(JSON.stringify({ detail: 'Authentication required' }), { status: 401, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } }));
		throw redirect(303, `/login?next=${encodeURIComponent(`${event.url.pathname}${event.url.search}`)}`);
	}
	const boundProfile = event.request.headers.has('x-bookward-profile') || mutation ||
		(path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'link');
	if (boundProfile) {
		const expected = await submittedProfile(event.request);
		if (!expected || expected !== user.profile_id) {
			return withSecurityHeaders(new Response(JSON.stringify({ detail: 'This page belongs to a different profile. Reload before continuing.' }), {
				status: 409, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }
			}));
		}
	}
	event.locals.user = user;
	if (user.must_change_password && path !== '/account' && !path.startsWith('/auth/')) throw redirect(303, '/account?force_password_change=1');
	event.cookies.set(SESSION_COOKIE, session, {
		httpOnly: true, secure: event.url.protocol === 'https:', sameSite: 'lax', path: '/', maxAge: 24 * 60 * 60
	});
	const response = await withSessionToken(session, () => resolve(event));
	response.headers.set('cache-control', 'private, no-store');
	return withSecurityHeaders(response);
};
