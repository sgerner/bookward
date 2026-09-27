import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engine } from '$lib/server/engine';

type CallbackResult = { linked?: boolean; session_token?: string; max_age?: number };

export const GET: RequestHandler = async ({ url, cookies }) => {
	const stateCookie = cookies.get('oidc_state');
	const intent = cookies.get('oidc_intent') === 'link' ? 'link' : 'login';
	cookies.delete('oidc_state', { path: '/auth/oidc' });
	cookies.delete('oidc_intent', { path: '/auth/oidc' });
	const providerError = url.searchParams.get('error');
	if (providerError) {
		throw redirect(303, intent === 'link' ? '/account?link_error=1' : '/login?error=sso');
	}
	const code = url.searchParams.get('code');
	const state = url.searchParams.get('state');
	if (!stateCookie || !code || !state) {
		throw redirect(303, intent === 'link' ? '/account?link_error=1' : '/login?error=sso');
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
			throw redirect(303, '/');
		}
		if (result.linked) throw redirect(303, '/account?linked=1');
		throw redirect(303, '/login?error=sso');
	} catch (error) {
		if (error && typeof error === 'object' && 'status' in error && 'location' in error) throw error;
		if (intent === 'link') throw redirect(303, '/account?link_error=1');
		if (error instanceof EngineError && error.status === 403) throw redirect(303, '/login?error=unlinked');
		throw redirect(303, '/login?error=sso');
	}
};
