import { fail as kitFail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '#lib/server/engine.js';

type IdentityInfo = { id: number; issuer: string; email: string; created_at: string };
type AuthMethods = { password_enabled: boolean; has_local_username?: boolean; oidc_identities: IdentityInfo[] };
type AuthConfig = { oidc_enabled: boolean };
const fail = (status: number, data: { message: string }) => kitFail(status, { ...data, error: true as const });

function ownsLocalUsername(username: string | undefined, explicitFlag?: boolean): boolean {
	if (typeof explicitFlag === 'boolean') return explicitFlag;
	const normalized = (username || '').trim().toLowerCase();
	return Boolean(normalized) && !normalized.startsWith('oidc:');
}

export const load: PageServerLoad = async ({ locals, url }) => {
	if (!locals.user) throw redirect(303, '/login');
	const mustChangePassword = Boolean(locals.user.must_change_password);
	if (!mustChangePassword && url.searchParams.get('force_password_change') === '1') {
		throw redirect(303, '/account');
	}
	const [methods, config] = await Promise.all([
		engine<AuthMethods>('/auth/identities'),
		engine<AuthConfig>('/auth/config')
	]);
	const hasLocalUsername = ownsLocalUsername(
		locals.user.username,
		methods.has_local_username ?? locals.user.has_local_username
	);
	return {
		user: {
			...locals.user,
			username: hasLocalUsername ? locals.user.username : '',
			has_local_username: hasLocalUsername
		},
		methods: { ...methods, has_local_username: hasLocalUsername },
		oidcEnabled: config.oidc_enabled,
		forcePasswordChange: mustChangePassword,
		reauthenticationRequired: url.searchParams.get('reauth') === '1',
		identityLinked: url.searchParams.get('linked') === '1',
		linkError: url.searchParams.get('link_error') === '1'
	};
};

export const actions: Actions = {
	changePassword: async ({ request, cookies, url, locals }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '').trim();
		const current_password = String(form.get('current_password') || '');
		const new_password = String(form.get('new_password') || '');
		const hasLocalUsername = locals.user
			? ownsLocalUsername(locals.user.username, locals.user.has_local_username)
			: Boolean(!username);
		if (!hasLocalUsername && !username) {
			return fail(400, { message: 'Choose a username for local sign-in.' });
		}
		if (username) {
			if (username.length > 160 || Array.from(username).some((ch) => ch.charCodeAt(0) < 32)) {
				return fail(400, { message: 'Enter a valid username.' });
			}
			if (username.toLowerCase().startsWith('oidc:')) {
				return fail(400, { message: "Usernames cannot start with 'oidc:'." });
			}
		}
		if (new_password.length < 12 || new_password.length > 1024) {
			return fail(400, { message: 'Use a password between 12 and 1024 characters.' });
		}
		const wasForcedReset =
			Boolean(locals.user?.must_change_password) || url.searchParams.get('force_password_change') === '1';
		let result: { session_token: string; max_age: number };
		try {
			const payload = username
				? { username, current_password, new_password }
				: { current_password, new_password };
			result = await engine<{ session_token: string; max_age: number }>('/auth/password/change', {
				method: 'POST',
				body: JSON.stringify(payload)
			});
		} catch (error) {
			return fail(error instanceof EngineError ? error.status : 503, {
				message: error instanceof Error ? error.message : 'Password could not be updated.'
			});
		}
		cookies.set('bookward_session', result.session_token, {
			httpOnly: true,
			secure: url.protocol === 'https:',
			sameSite: 'lax',
			path: '/',
			maxAge: result.max_age
		});
		if (wasForcedReset) throw redirect(303, '/');
		return { message: 'Password updated.' };
	},
	unlink: async ({ request }) => {
		const form = await request.formData();
		const id = Number(form.get('id'));
		if (!Number.isSafeInteger(id) || id <= 0) return fail(400, { message: 'Choose a valid login method.' });
		try {
			await engine(`/auth/identities/${id}`, { method: 'DELETE' });
			return { message: 'SSO login disconnected.' };
		} catch (error) {
			return fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Login method could not be removed.' });
		}
	}
};
