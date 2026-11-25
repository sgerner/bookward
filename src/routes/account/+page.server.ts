import { fail as kitFail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { SESSION_COOKIE, sessionCookieOptions } from '#lib/server/cookies.js';
import { EngineError, engine } from '#lib/server/engine.js';

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
	}
) => kitFail(status, { fieldErrors: {}, ...data, error: true as const });

function ownsLocalUsername(username: string | undefined, explicitFlag?: boolean): boolean {
	if (typeof explicitFlag === 'boolean') return explicitFlag;
	const normalized = (username || '').trim().toLowerCase();
	return Boolean(normalized) && !normalized.startsWith('oidc:');
}

export const load: PageServerLoad = async ({ locals, url }) => {
	if (!locals.user) throw redirect(303, '/login');
	const [methods, config] = await Promise.all([
		engine<AuthMethods>('/auth/identities'),
		engine<AuthConfig>('/auth/config')
	]);
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
	changePassword: async ({ request, cookies, url, locals }) => {
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
		try {
			const payload = !hasLocalUsername
				? { username, current_password, new_password }
				: { current_password, new_password };
			const result = await engine<{ session_token: string; max_age: number }>('/auth/password/change', {
				method: 'POST',
				body: JSON.stringify(payload)
			});
			cookies.set(SESSION_COOKIE, result.session_token, sessionCookieOptions(url, result.max_age));
			return { formId: 'changePassword', message: 'Password updated.' };
		} catch (error) {
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
	}
};
