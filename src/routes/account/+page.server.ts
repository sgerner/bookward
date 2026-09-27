import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '$lib/server/engine';

type IdentityInfo = { id: number; issuer: string; email: string; created_at: string };
type AuthMethods = { password_enabled: boolean; oidc_identities: IdentityInfo[] };
type AuthConfig = { oidc_enabled: boolean };

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
	changePassword: async ({ request, cookies, url }) => {
		const form = await request.formData();
		const current_password = String(form.get('current_password') || '');
		const new_password = String(form.get('new_password') || '');
		if (new_password.length < 12) return fail(400, { message: 'Use at least 12 characters.' });
		try {
			const result = await engine<{ session_token: string; max_age: number }>('/auth/password/change', {
				method: 'POST', body: JSON.stringify({ current_password, new_password })
			});
			cookies.set('bookward_session', result.session_token, {
				httpOnly: true, secure: url.protocol === 'https:', sameSite: 'lax', path: '/', maxAge: result.max_age
			});
			return { message: 'Password updated.' };
		} catch (error) {
			return fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Password could not be updated.' });
		}
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
