import type { Handle } from '@sveltejs/kit/hooks';
import { redirect } from '@sveltejs/kit';
import { timingSafeEqual } from 'node:crypto';
import { ENGINE_UNAVAILABLE_MESSAGE, EngineError, engine } from '#lib/server/engine.js';
import { withSessionToken } from '#lib/server/request-context.js';

const SESSION_COOKIE = 'bookward_session';
const ORIGIN_ERROR_DETAIL = 'Request origin could not be verified';
const AUTH_REQUIRED_DETAIL = 'Authentication required. Sign in again to continue.';
const STALE_PROFILE_DETAIL = 'This page belongs to a different profile. Reload before continuing.';

type SessionLookup =
	| { status: 'authenticated'; user: NonNullable<App.Locals['user']> }
	| { status: 'unauthenticated' }
	| { status: 'unavailable' };

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

function escapeHtml(value: string) {
	return value
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}

function jsonError(status: number, detail: string) {
	return withSecurityHeaders(
		new Response(JSON.stringify({ detail, message: detail }), {
			status,
			headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }
		})
	);
}

function htmlError(status: number, heading: string, detail: string, actionLabel: string, actionHref: string) {
	const safeHeading = escapeHtml(heading);
	const safeDetail = escapeHtml(detail);
	const safeActionLabel = escapeHtml(actionLabel);
	const safeActionHref = escapeHtml(actionHref);
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${status} — Bookward</title>
<style>
body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #0f111a; color: #f4f5f8; }
main { max-width: 34rem; min-height: 100dvh; margin: 0 auto; padding: 3rem 1.5rem; box-sizing: border-box; display: grid; align-content: center; gap: 1rem; }
.card { border-radius: 1.25rem; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.05); padding: 1.75rem; }
.badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 999px; background: rgba(245,158,11,0.18); color: #fcd34d; font-size: 0.8rem; font-weight: 600; }
h1 { margin: 0.85rem 0 0.5rem; font-size: 1.5rem; }
p { margin: 0 0 1.25rem; line-height: 1.6; color: #cbd5e1; }
a.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 2.75rem; padding: 0 1.1rem; border-radius: 0.75rem; background: #7169f5; color: #ffffff; font-weight: 600; text-decoration: none; }
</style>
</head>
<body>
<main id="main-content" tabindex="-1">
<div class="card" role="alert">
<span class="badge">${status}</span>
<h1>${safeHeading}</h1>
<p>${safeDetail}</p>
<a class="btn" href="${safeActionHref}">${safeActionLabel}</a>
</div>
</main>
</body>
</html>`;
	return withSecurityHeaders(
		new Response(html, {
			status,
			headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' }
		})
	);
}

function isUnhydratedFormPost(request: Request, path: string) {
	if (path.startsWith('/api/')) return false;
	if (request.headers.get('x-sveltekit-action') === 'true') return false;
	const contentType = (request.headers.get('content-type') || '').toLowerCase();
	if (contentType.includes('application/json')) return false;
	const accept = (request.headers.get('accept') || '').toLowerCase();
	return accept.includes('text/html') && !accept.startsWith('application/json');
}

function wantsJsonAuthError(request: Request, path: string) {
	return path.startsWith('/api/') || (request.headers.get('x-sveltekit-action') === 'true' && !path.startsWith('/auth/'));
}

async function sessionUser(token: string): Promise<SessionLookup> {
	try {
		const user = await engine<NonNullable<App.Locals['user']>>('/auth/me', {
			headers: { 'x-bookward-session': token }
		});
		return { status: 'authenticated', user };
	} catch (error) {
		if (error instanceof EngineError && error.status === 401) {
			return { status: 'unauthenticated' };
		}
		return { status: 'unavailable' };
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
			return jsonError(403, ORIGIN_ERROR_DETAIL);
		}
	}
	if (session && path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'link') {
		const origin = event.request.headers.get('origin');
		const fetchSite = event.request.headers.get('sec-fetch-site');
		if ((origin && origin !== event.url.origin) || (!origin && fetchSite !== 'same-origin')) {
			return withSecurityHeaders(new Response(ORIGIN_ERROR_DETAIL, { status: 403 }));
		}
	}

	if (path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'login') {
		return withSecurityHeaders(await resolve(event));
	}

	const callback = path === '/auth/oidc/callback';
	if (path === '/login' && session) {
		const lookup = await sessionUser(session);
		if (lookup.status === 'authenticated') throw redirect(303, '/');
		if (lookup.status === 'unauthenticated') {
			event.cookies.delete(SESSION_COOKIE, { path: '/' });
		} else {
			event.locals.engineUnavailable = true;
		}
		const response = await resolve(event);
		if (event.locals.engineUnavailable) response.headers.set('cache-control', 'private, no-store');
		return withSecurityHeaders(response);
	}

	if (!session && path !== '/login' && !callback) {
		if (wantsJsonAuthError(event.request, path)) return jsonError(401, AUTH_REQUIRED_DETAIL);
		if (path === '/auth/logout') throw redirect(303, '/login?manual=1');
		const next = `${event.url.pathname}${event.url.search}`;
		throw redirect(303, `/login?next=${encodeURIComponent(next)}`);
	}

	if (!session) return withSecurityHeaders(await resolve(event));

	const lookup = await sessionUser(session);
	if (lookup.status === 'unauthenticated') {
		event.cookies.delete(SESSION_COOKIE, { path: '/' });
		if (callback) return withSecurityHeaders(await resolve(event));
		if (wantsJsonAuthError(event.request, path)) return jsonError(401, AUTH_REQUIRED_DETAIL);
		if (path === '/auth/logout') throw redirect(303, '/login?manual=1');
		throw redirect(303, `/login?next=${encodeURIComponent(`${event.url.pathname}${event.url.search}`)}`);
	}
	if (lookup.status === 'unavailable') {
		if (path.startsWith('/api/') || mutation) {
			return jsonError(503, ENGINE_UNAVAILABLE_MESSAGE);
		}
		if (path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'link') {
			return withSecurityHeaders(new Response(ENGINE_UNAVAILABLE_MESSAGE, { status: 503, headers: { 'cache-control': 'no-store' } }));
		}
		event.locals.engineUnavailable = true;
		const response = await withSessionToken(session, () => resolve(event));
		response.headers.set('cache-control', 'private, no-store');
		return withSecurityHeaders(response);
	}
	const user = lookup.user;
	const boundProfile = event.request.headers.has('x-bookward-profile') || mutation ||
		(path === '/auth/oidc/start' && event.url.searchParams.get('intent') === 'link');
	if (boundProfile) {
		const expected = await submittedProfile(event.request);
		if (!expected || expected !== user.profile_id) {
			if (mutation && isUnhydratedFormPost(event.request, path)) {
				return htmlError(409, 'Profile changed', STALE_PROFILE_DETAIL, 'Reload Bookward', '/');
			}
			return jsonError(409, STALE_PROFILE_DETAIL);
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
