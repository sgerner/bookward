import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '$lib/server/engine';

type AuthConfig = { setup_required: boolean; local_login_enabled: boolean; oidc_enabled: boolean };
type SessionResult = { session_token: string; max_age: number };

function safeNext(value: FormDataEntryValue | null) {
	const next = typeof value === 'string' ? value : '/';
	return next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/\\') ? next : '/';
}

export const load: PageServerLoad = async ({ url }) => {
	const config = await engine<AuthConfig>('/auth/config');
	const error = url.searchParams.get('error');
	return { ...config, next: safeNext(url.searchParams.get('next')), error: error === 'unlinked' ? 'This SSO identity is not linked yet. Sign in with your local account and link it from Account settings.' : error === 'sso' ? 'Single sign-on could not be completed. Try again or use your username and password.' : '' };
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
