const env = process.env;
import { error as httpError, fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { SESSION_COOKIE, sessionCookieOptions } from '#lib/server/cookies.js';
import { EngineError, engine } from '#lib/server/engine.js';
import { formatWaitDuration } from '#lib/lockout-ui.js';
import { currentClientIp, resolveClientAddress, withRequestContext } from '#lib/server/request-context.js';
import { safeNext } from '#lib/server/safe-next.js';

type AuthConfig = { setup_required: boolean; local_login_enabled: boolean; oidc_enabled: boolean };
type SessionResult = { session_token: string; max_age: number };

export const load: PageServerLoad = async ({ url, locals }) => {
	if (locals?.engineUnavailable) {
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
	let config: AuthConfig;
	try {
		config = await engine<AuthConfig>('/auth/config');
	} catch {
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
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

const formFail = (
	status: number,
	formId: 'login' | 'setup',
	message: string,
	fieldErrors: Record<string, string> = {},
	values?: Record<string, string>
) =>
	fail(status, {
		formId,
		error: true as const,
		message,
		fieldErrors,
		...(values ? { values } : {})
	});

const hasControlCharacters = (value: string) =>
	[...value].some((character) => character.charCodeAt(0) < 32);

export const actions: Actions = {
	login: async (event) => {
		const { request, cookies, url, setHeaders } = event;
		const form = await request.formData();
		const username = String(form.get('username') || '');
		const cleanUsername = username.trim();
		const password = String(form.get('password') || '');
		const fieldErrors: Record<string, string> = {};
		if (!cleanUsername) fieldErrors.username = 'Enter your username.';
		if (!password) fieldErrors.password = 'Enter your password.';
		if (Object.keys(fieldErrors).length > 0) {
			const summary =
				!cleanUsername && !password
					? 'Enter your username and password.'
					: (fieldErrors.username ?? fieldErrors.password!);
			return formFail(400, 'login', summary, fieldErrors, { username: cleanUsername });
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		let result: SessionResult;
		try {
			result = await withRequestContext({ clientIp }, () =>
				engine<SessionResult>('/auth/login', {
					method: 'POST',
					body: JSON.stringify({ username, password })
				})
			);
		} catch (error) {
			if (error instanceof EngineError && error.status === 401) {
				return fail(401, {
					formId: 'login' as const,
					error: true as const,
					credentialError: true as const,
					message: 'Username or password is incorrect.',
					fieldErrors: {} as Record<string, string>,
					values: { username: cleanUsername }
				});
			}
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					formId: 'login' as const,
					error: true as const,
					rateLimited: true as const,
					retryAfterSeconds,
					message: `Too many sign-in attempts from this address. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					fieldErrors: {} as Record<string, string>,
					values: { username: cleanUsername }
				});
			}
			return formFail(
				error instanceof EngineError ? error.status : 503,
				'login',
				error instanceof Error ? error.message : 'Sign-in is unavailable.',
				{},
				{ username: cleanUsername }
			);
		}
		cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
		throw redirect(303, safeNext(form.get('next')));
	},
	setup: async ({ request, cookies, url }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '');
		const cleanUsername = username.trim();
		const password = String(form.get('password') || '');
		const fieldErrors: Record<string, string> = {};
		if (!cleanUsername) {
			fieldErrors.username = 'Enter a username.';
		} else if (cleanUsername.length > 160 || hasControlCharacters(cleanUsername)) {
			fieldErrors.username = 'Enter a valid username.';
		}
		if (!password) {
			fieldErrors.password = 'Enter a password.';
		} else if (password.length < 12 || password.length > 1024) {
			fieldErrors.password = 'Use a password between 12 and 1024 characters.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			const summary =
				!cleanUsername && !password
					? 'Enter a username and password.'
					: (fieldErrors.username ?? fieldErrors.password!);
			return formFail(400, 'setup', summary, fieldErrors, { username: cleanUsername });
		}
		let result: SessionResult;
		try {
			result = await engine<SessionResult>('/auth/setup', {
				method: 'POST',
				body: JSON.stringify({ username, password })
			});
		} catch (error) {
			const status = error instanceof EngineError ? error.status : 503;
			const msg = error instanceof Error ? error.message : 'Account setup is unavailable.';
			const mappedFieldErrors: Record<string, string> =
				status === 400
					? /username/i.test(msg)
						? { username: msg }
						: /password/i.test(msg)
							? { password: msg }
							: {}
					: {};
			return formFail(status, 'setup', msg, mappedFieldErrors, { username: cleanUsername });
		}
		cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
		throw redirect(303, '/');
	}
};
