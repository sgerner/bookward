import { error as httpError, fail as kitFail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
	SESSION_COOKIE,
	sessionCookieDeleteOptions,
	sessionCookieOptions
} from '#lib/server/cookies.js';
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
type AuthMethods = {
	password_enabled: boolean;
	has_local_username?: boolean;
	enrollment_verified?: boolean;
	oidc_identities: IdentityInfo[];
};
type AuthConfig = { oidc_enabled: boolean };
const hasControlCharacters = (value: string) =>
	[...value].some((character) => character.charCodeAt(0) < 32);

const fail = (
	status: number,
	data: {
		formId: string;
		message: string;
		fieldErrors?: Record<string, string>;
		values?: Record<string, string>;
		rateLimited?: true;
		retryAfterSeconds?: number;
	}
) => kitFail(status, { fieldErrors: {}, ...data, error: true as const });

function ownsLocalUsername(username: string | undefined, explicitFlag?: boolean): boolean {
	if (typeof explicitFlag === 'boolean') return explicitFlag;
	const normalized = (username || '').trim().toLowerCase();
	return Boolean(normalized) && !normalized.startsWith('oidc:');
}

export const load: PageServerLoad = async ({ locals, url }) => {
	if (locals?.engineUnavailable) {
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
	if (!locals.user) throw redirect(303, '/login');
	let methods: AuthMethods;
	let config: AuthConfig;
	try {
		[methods, config] = await Promise.all([
			engine<AuthMethods>('/auth/identities'),
			engine<AuthConfig>('/auth/config')
		]);
	} catch (error) {
		if (error instanceof EngineError && error.status === 401) throw redirect(303, '/login');
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
	const hasLocalUsername = ownsLocalUsername(
		locals.user.username,
		methods.has_local_username ?? locals.user.has_local_username
	);
	const enrollmentVerified = Boolean(methods.enrollment_verified);
	return {
		user: {
			...locals.user,
			username: hasLocalUsername ? locals.user.username : '',
			has_local_username: hasLocalUsername
		},
		methods: { ...methods, has_local_username: hasLocalUsername, enrollment_verified: enrollmentVerified },
		oidcEnabled: config.oidc_enabled,
		enrollmentVerified,
		forcePasswordChange: url.searchParams.get('force_password_change') === '1',
		reauthenticationRequired: url.searchParams.get('reauth') === '1',
		identityLinked: url.searchParams.get('linked') === '1',
		linkError: url.searchParams.get('link_error') === '1',
		enrollConfirmed: url.searchParams.get('enroll') === '1',
		enrollError: url.searchParams.get('enroll_error') === '1'
	};
};

export const actions: Actions = {
	changePassword: async (event) => {
		const { request, cookies, locals, url, setHeaders } = event;
		const form = await request.formData();
		const hasUsernameField = form.has('username');
		const username = String(form.get('username') || '').trim();
		const hasCurrentPassword = form.has('current_password');
		const current_password = String(form.get('current_password') || '');
		const new_password = String(form.get('new_password') || '');
		const hasLocalUsername = locals?.user
			? ownsLocalUsername(locals.user.username, locals.user.has_local_username)
			: !hasUsernameField;
		const fieldErrors: Record<string, string> = {};
		if (!hasLocalUsername) {
			if (!username) {
				fieldErrors.username = 'Choose a username for local sign-in.';
			} else if (username.length > 160 || hasControlCharacters(username)) {
				fieldErrors.username = 'Enter a valid username.';
			} else if (username.toLowerCase().startsWith('oidc:')) {
				fieldErrors.username = "Usernames cannot start with 'oidc:'.";
			}
		} else if (
			username &&
			username.toLowerCase() !== (locals?.user?.username || '').trim().toLowerCase()
		) {
			fieldErrors.username = 'Username cannot be changed once set.';
		}
		if (hasLocalUsername && hasCurrentPassword && !current_password) {
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
				message: Object.values(fieldErrors)[0]!,
				fieldErrors,
				...(!hasLocalUsername ? { values: { username } } : {})
			});
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		try {
			const payload = !hasLocalUsername
				? { username, current_password, new_password }
				: { current_password, new_password };
			const result = await withRequestContext({ sessionToken: currentSessionToken(), clientIp }, () =>
				engine<{
					session_token: string;
					max_age: number;
					username?: string;
					has_local_username?: boolean;
				}>('/auth/password/change', {
					method: 'POST',
					body: JSON.stringify(payload)
				})
			);
			cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
			setCurrentSessionToken(result.session_token);
			if (locals?.user) {
				locals.user = {
					...locals.user,
					must_change_password: false,
					...(typeof result.username === 'string' ? { username: result.username } : {}),
					...(typeof result.has_local_username === 'boolean'
						? { has_local_username: result.has_local_username }
						: {})
				};
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
					retryAfterSeconds,
					...(!hasLocalUsername ? { values: { username } } : {})
				});
			}
			const status = error instanceof EngineError ? error.status : 503;
			const msg = error instanceof Error ? error.message : 'Password could not be updated.';
			const mappedFieldErrors: Record<string, string> = /current password/i.test(msg)
				? { current_password: msg }
				: status === 409 || (status === 400 && /username/i.test(msg))
					? { username: msg }
					: status === 400 && /password/i.test(msg)
						? { new_password: msg }
						: {};
			return fail(status, {
				formId: 'changePassword',
				message: msg,
				fieldErrors: mappedFieldErrors,
				...(!hasLocalUsername ? { values: { username } } : {})
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
	},
	resetHistory: async (event) => {
		const { request, setHeaders } = event;
		const form = await request.formData();
		const hasCurrentPassword = form.has('current_password');
		const current_password = String(form.get('current_password') || '');
		const confirm = String(form.get('confirm') || '').trim();
		const fieldErrors: Record<string, string> = {};
		if (hasCurrentPassword && !current_password) {
			fieldErrors.current_password = 'Enter your current password.';
		}
		if (confirm !== 'RESET') {
			fieldErrors.confirm = 'Type RESET to confirm resetting your reading history.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			return fail(400, {
				formId: 'resetHistory',
				message: fieldErrors.confirm ?? fieldErrors.current_password!,
				fieldErrors
			});
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		try {
			await withRequestContext({ sessionToken: currentSessionToken(), clientIp }, () =>
				engine(
					'/auth/account/reset-history',
					{ method: 'POST', body: JSON.stringify({ current_password, confirm }) },
					10 * 60_000
				)
			);
			return {
				formId: 'resetHistory',
				message: 'Reading history and recommendation evidence were reset.'
			};
		} catch (error) {
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					formId: 'resetHistory',
					message: `Too many password attempts. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					rateLimited: true as const,
					retryAfterSeconds
				});
			}
			const status = error instanceof EngineError ? error.status : 503;
			const msg =
				error instanceof Error ? error.message : 'Reading history could not be reset.';
			const mappedFieldErrors: Record<string, string> = /current password/i.test(msg)
				? { current_password: msg }
				: status === 400 && /confirm resetting/i.test(msg)
					? { confirm: msg }
					: {};
			return fail(status, {
				formId: 'resetHistory',
				message: msg,
				fieldErrors: mappedFieldErrors
			});
		}
	},
	deleteAccount: async (event) => {
		const { request, cookies, url, setHeaders } = event;
		const form = await request.formData();
		const hasCurrentPassword = form.has('current_password');
		const current_password = String(form.get('current_password') || '');
		const confirm = String(form.get('confirm') || '').trim();
		const fieldErrors: Record<string, string> = {};
		if (hasCurrentPassword && !current_password) {
			fieldErrors.current_password = 'Enter your current password.';
		}
		if (confirm !== 'DELETE') {
			fieldErrors.confirm = 'Type DELETE to confirm deleting your account.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			return fail(400, {
				formId: 'deleteAccount',
				message: fieldErrors.confirm ?? fieldErrors.current_password!,
				fieldErrors
			});
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		try {
			await withRequestContext({ sessionToken: currentSessionToken(), clientIp }, () =>
				engine(
					'/auth/account',
					{ method: 'DELETE', body: JSON.stringify({ current_password, confirm }) },
					10 * 60_000
				)
			);
		} catch (error) {
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					formId: 'deleteAccount',
					message: `Too many password attempts. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					rateLimited: true as const,
					retryAfterSeconds
				});
			}
			const status = error instanceof EngineError ? error.status : 503;
			if (error instanceof EngineError && error.status === 503) {
				cookies.delete(SESSION_COOKIE, sessionCookieDeleteOptions(url));
			}
			const msg = error instanceof Error ? error.message : 'Account could not be deleted.';
			const mappedFieldErrors: Record<string, string> = /current password/i.test(msg)
				? { current_password: msg }
				: status === 400 && /confirm deleting/i.test(msg)
					? { confirm: msg }
					: {};
			return fail(status, {
				formId: 'deleteAccount',
				message: msg,
				fieldErrors: mappedFieldErrors
			});
		}
		cookies.delete(SESSION_COOKIE, sessionCookieDeleteOptions(url));
		throw redirect(303, '/login?manual=1');
	}
};
