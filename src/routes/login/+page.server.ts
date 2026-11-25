const env = process.env;
import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { SESSION_COOKIE, sessionCookieOptions } from '#lib/server/cookies.js';
import { EngineError, engine } from '#lib/server/engine.js';
import { formatWaitDuration } from '#lib/lockout-ui.js';
import { currentClientIp, resolveClientAddress, withRequestContext } from '#lib/server/request-context.js';
import { safeNext } from '#lib/server/safe-next.js';

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
	login: async (event) => {
		const { request, cookies, url, setHeaders } = event;
		const form = await request.formData();
		const username = String(form.get('username') || '');
		const password = String(form.get('password') || '');
		if (!username || !password) return fail(400, { message: 'Enter your username and password.' });
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		let result: SessionResult;
		try {
			result = await withRequestContext({ clientIp }, () =>
				engine<SessionResult>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) })
			);
		} catch (error) {
			if (error instanceof EngineError && error.status === 401) {
				return fail(401, { message: 'Username or password is incorrect.' });
			}
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					message: `Too many sign-in attempts from this address. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					rateLimited: true as const,
					retryAfterSeconds
				});
			}
			return fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Sign-in is unavailable.' });
		}
		cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
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
		cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
		throw redirect(303, '/');
	}
};
