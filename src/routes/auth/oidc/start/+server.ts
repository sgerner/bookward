import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engine } from '$lib/server/engine';
import { safeNext } from '$lib/server/safe-next';

function loginErrorLocation(next: string) {
	const query = new URLSearchParams({ error: 'sso' });
	if (next !== '/') query.set('next', next);
	return `/login?${query}`;
}

export const GET: RequestHandler = async ({ url, cookies }) => {
	const intent = url.searchParams.get('intent') === 'link' ? 'link' : 'login';
	const next = intent === 'login' ? safeNext(url.searchParams.get('next')) : '/';
	let result: { authorization_url: string; csrf: string };
	try {
		result = await engine<{ authorization_url: string; csrf: string }>(`/auth/oidc/start?intent=${intent}`);
	} catch (error) {
		if (intent === 'link' && error instanceof EngineError && error.status === 401) {
			throw redirect(303, '/account?reauth=1');
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
	cookies.set('oidc_state', result.csrf, {
		httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/auth/oidc', maxAge: 600
	});
	cookies.set('oidc_intent', intent, {
		httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/auth/oidc', maxAge: 600
	});
	if (intent === 'login') {
		cookies.set('oidc_next', next, {
			httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/auth/oidc', maxAge: 600
		});
	} else {
		cookies.delete('oidc_next', { path: '/auth/oidc' });
	}
	throw redirect(302, destination.toString());
};
