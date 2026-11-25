import { fail as kitFail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { SESSION_COOKIE, sessionCookieOptions } from '#lib/server/cookies.js';
import { EngineError, engine } from '#lib/server/engine.js';
import { formatWaitDuration } from '#lib/lockout-ui.js';
import {
	currentClientIp,
	currentSessionToken,
	resolveClientAddress,
	setCurrentSessionToken,
	withRequestContext
} from '#lib/server/request-context.js';

type IdentityInfo = { id: number; issuer: string; email: string; created_at: string };
type AuthMethods = { password_enabled: boolean; oidc_identities: IdentityInfo[] };
type AuthConfig = { oidc_enabled: boolean };
const fail = (
	status: number,
	data: {
		formId: string;
		message: string;
		fieldErrors?: Record<string, string>;
		rateLimited?: true;
		retryAfterSeconds?: number;
	}
) => kitFail(status, { fieldErrors: {}, ...data, error: true as const });

export const load: PageServerLoad = async ({ locals, url }) => {
	if (!locals.user) throw redirect(303, '/login');
	const [methods, config] = await Promise.all([
		engine<AuthMethods>('/auth/identities'),
		engine<AuthConfig>('/auth/config')
	]);
	return {
		user: locals.user,
		methods,
		oidcEnabled: config.oidc_enabled,
		forcePasswordChange: url.searchParams.get('force_password_change') === '1',
		reauthenticationRequired: url.searchParams.get('reauth') === '1',
		identityLinked: url.searchParams.get('linked') === '1',
		linkError: url.searchParams.get('link_error') === '1'
	};
};

export const actions: Actions = {
	changePassword: async (event) => {
		const { request, cookies, locals, url, setHeaders } = event;
		const form = await request.formData();
		const hasCurrentPassword = form.has('current_password');
		const current_password = String(form.get('current_password') || '');
		const new_password = String(form.get('new_password') || '');
		const fieldErrors: Record<string, string> = {};
		if (hasCurrentPassword && !current_password) {
			fieldErrors.current_password = 'Enter your current password.';
		}
		if (!new_password) {
			fieldErrors.new_password = 'Enter a new password.';
		} else if (new_password.length < 12 || new_password.length > 1024) {
			fieldErrors.new_password = 'Use a password between 12 and 1024 characters.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			return fail(400, {
				formId: 'changePassword',
				message: fieldErrors.current_password ?? fieldErrors.new_password!,
				fieldErrors
			});
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		try {
			const result = await withRequestContext({ sessionToken: currentSessionToken(), clientIp }, () =>
				engine<{ session_token: string; max_age: number }>('/auth/password/change', {
					method: 'POST', body: JSON.stringify({ current_password, new_password })
				})
			);
			cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
			setCurrentSessionToken(result.session_token);
			if (locals?.user) {
				locals.user = { ...locals.user, must_change_password: false };
			}
			return { formId: 'changePassword', message: 'Password updated.' };
		} catch (error) {
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					formId: 'changePassword',
					message: `Too many password attempts. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					rateLimited: true as const,
					retryAfterSeconds
				});
			}
			const status = error instanceof EngineError ? error.status : 503;
			const msg = error instanceof Error ? error.message : 'Password could not be updated.';
			const mappedFieldErrors: Record<string, string> = /current password/i.test(msg)
				? { current_password: msg }
				: status === 400 && /password/i.test(msg)
					? { new_password: msg }
					: {};
			return fail(status, {
				formId: 'changePassword',
				message: msg,
				fieldErrors: mappedFieldErrors
			});
		}
	},
	unlink: async ({ request }) => {
		const form = await request.formData();
		const id = Number(form.get('id'));
		if (!Number.isSafeInteger(id) || id <= 0) {
			return fail(400, { formId: 'unlink', message: 'Choose a valid login method.' });
		}
		try {
			await engine(`/auth/identities/${id}`, { method: 'DELETE' });
			return { formId: 'unlink', message: 'SSO login disconnected.' };
		} catch (error) {
			return fail(error instanceof EngineError ? error.status : 503, {
				formId: 'unlink',
				message: error instanceof Error ? error.message : 'Login method could not be removed.'
			});
		}
	}
};
