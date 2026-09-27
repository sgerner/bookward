import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '$lib/server/engine';
import { safeNext } from '$lib/server/safe-next';
import { env } from '$env/dynamic/private';

type AuthConfig = { setup_required: boolean; local_login_enabled: boolean; oidc_enabled: boolean };
type SessionResult = { session_token: string; max_age: number };

export const load: PageServerLoad = async ({ url }) => {
	const config = await engine<AuthConfig>('/auth/config');
	const next = safeNext(url.searchParams.get('next'));
	const errorCode = url.searchParams.get('error');
	const error = errorCode === 'unlinked' ? 'This SSO identity is not linked yet. Sign in with your local account and link it from Account settings.' : errorCode === 'sso' ? 'Single sign-on could not be completed. Try again or use your username and password.' : '';
	const autoSso = env.OIDC_AUTO_LOGIN?.trim().toLowerCase() === 'true' && config.oidc_enabled;
	const manualLogin = url.searchParams.get('manual') === '1';
	const ssoFailed = errorCode === 'sso' || errorCode === 'unlinked';
	if (autoSso && !config.setup_required && !manualLogin && !ssoFailed) {
		throw redirect(303, `/auth/oidc/start?intent=login&next=${encodeURIComponent(next)}`);
	}
	return { ...config, next, auto_sso: autoSso, manual_login: manualLogin, error };
};

export const actions: Actions = {
	login: async ({ request, cookies, url }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '');
		const password = String(form.get('password') || '');
		if (!username || !password) return fail(400, { message: 'Enter your username and password.' });
		let result: SessionResult;
		try {
			result = await engine<SessionResult>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
		} catch (error) {
			if (error instanceof EngineError && error.status === 401) return fail(401, { message: 'Username or password is incorrect.' });
			return fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Sign-in is unavailable.' });
		}
		cookies.set('bookward_session', result.session_token, {
			httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/',
			maxAge: result.max_age
		});
		throw redirect(303, safeNext(form.get('next')));
	},
	setup: async ({ request, cookies, url }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '');
		const password = String(form.get('password') || '');
		if (!username || !password) return fail(400, { message: 'Enter a username and password.' });
		let result: SessionResult;
		try {
			result = await engine<SessionResult>('/auth/setup', { method: 'POST', body: JSON.stringify({ username, password }) });
		} catch (error) {
			return fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Account setup is unavailable.' });
		}
		cookies.set('bookward_session', result.session_token, {
			httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/',
			maxAge: result.max_age
		});
		throw redirect(303, '/');
	}
};
