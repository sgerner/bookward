import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engine } from '$lib/server/engine';
import { safeNext } from '$lib/server/safe-next';

type CallbackResult = { linked?: boolean; session_token?: string; max_age?: number };

export const GET: RequestHandler = async ({ url, cookies }) => {
	const stateCookie = cookies.get('oidc_state');
	const intent = cookies.get('oidc_intent') === 'link' ? 'link' : 'login';
	const next = intent === 'login' ? safeNext(cookies.get('oidc_next')) : '/';
	cookies.delete('oidc_state', { path: '/auth/oidc' });
	cookies.delete('oidc_intent', { path: '/auth/oidc' });
	cookies.delete('oidc_next', { path: '/auth/oidc' });
	const loginLocation = (error: 'sso' | 'unlinked') => {
		const query = new URLSearchParams({ error });
		if (next !== '/') query.set('next', next);
		return `/login?${query}`;
	};
	const providerError = url.searchParams.get('error');
	if (providerError) {
		throw redirect(303, intent === 'link' ? '/account?link_error=1' : loginLocation('sso'));
	}
	const code = url.searchParams.get('code');
	const state = url.searchParams.get('state');
	if (!stateCookie || !code || !state) {
		throw redirect(303, intent === 'link' ? '/account?link_error=1' : loginLocation('sso'));
	}
	try {
		const query = new URLSearchParams({ code, state });
		const result = await engine<CallbackResult>(`/auth/oidc/callback?${query}`, {
			headers: { 'x-oidc-state': stateCookie }
		});
		if (result.session_token) {
			cookies.set('bookward_session', result.session_token, {
				httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/',
				maxAge: result.max_age
			});
			throw redirect(303, next);
		}
		if (result.linked) throw redirect(303, '/account?linked=1');
		throw redirect(303, loginLocation('sso'));
	} catch (error) {
		if (error && typeof error === 'object' && 'status' in error && 'location' in error) throw error;
		if (intent === 'link') throw redirect(303, '/account?link_error=1');
		if (error instanceof EngineError && error.status === 403) throw redirect(303, loginLocation('unlinked'));
		throw redirect(303, loginLocation('sso'));
	}
};
