import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { oidcCookieDeleteOptions, oidcCookieOptions } from '#lib/server/cookies.js';
import { EngineError, engine } from '#lib/server/engine.js';
import { safeNext } from '#lib/server/safe-next.js';

function loginErrorLocation(next: string) {
	const query = new URLSearchParams({ error: 'sso' });
	if (next !== '/') query.set('next', next);
	return `/login?${query}`;
}

export const GET: RequestHandler = async ({ url, cookies }) => {
	const rawIntent = url.searchParams.get('intent');
	const intent = rawIntent === 'link' || rawIntent === 'enroll' ? rawIntent : 'login';
	const next = intent === 'login' ? safeNext(url.searchParams.get('next')) : '/';
	let result: { authorization_url: string; csrf: string };
	try {
		result = await engine<{ authorization_url: string; csrf: string }>(`/auth/oidc/start?intent=${intent}`);
	} catch (error) {
		if (intent === 'link' && error instanceof EngineError && error.status === 401) {
			throw redirect(303, '/account?reauth=1');
		}
		if (intent === 'enroll' && error instanceof EngineError && error.status === 401) {
			throw redirect(303, '/login?next=%2Faccount');
		}
		if (intent === 'enroll' && error instanceof EngineError && error.status === 400) {
			throw redirect(303, '/account?enroll_error=1');
		}
		if (intent === 'login') throw redirect(303, loginErrorLocation(next));
		throw error;
	}
	let destination: URL;
	try {
		destination = new URL(result.authorization_url);
		if (destination.protocol !== 'https:' && destination.hostname !== 'localhost' && destination.hostname !== '127.0.0.1') {
			throw new Error('The SSO provider returned an insecure authorization URL.');
		}
	} catch {
		if (intent === 'login') throw redirect(303, loginErrorLocation(next));
		throw new Error('The SSO provider returned an invalid authorization URL.');
	}
	const cookieOptions = oidcCookieOptions(url);
	cookies.set('oidc_state', result.csrf, cookieOptions);
	cookies.set('oidc_intent', intent, cookieOptions);
	if (intent === 'login') {
		cookies.set('oidc_next', next, cookieOptions);
	} else {
		cookies.delete('oidc_next', oidcCookieDeleteOptions(url));
	}
	throw redirect(302, destination.toString(), { external: true });
};
