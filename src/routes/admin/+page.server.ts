import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '#lib/server/engine.js';

type Account = {
	id: string;
	profile_id: string;
	username: string;
	has_local_username?: boolean;
	display_name: string;
	role: 'admin' | 'user';
	status: 'active' | 'disabled';
};
type IdentityBackupStatus = {
	enabled: boolean;
	interval_hours: number;
	last_success_at: string | null;
	last_attempt_at: string | null;
	last_error: string | null;
	last_restore_at: string | null;
	backups: Array<{ id: string; created_at: string; size_bytes: number }>;
};
const validationFailure = (message: string) => fail(400, { message, error: true as const });
const failure = (error: unknown) => fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Account operation failed.', error: true as const });

export const load: PageServerLoad = async ({ locals }) => {
	if (!locals.user) throw redirect(303, '/login');
	if (locals.user.role !== 'admin') throw redirect(303, '/');
	const [result, identityBackup] = await Promise.all([
		engine<{ accounts: Account[] }>('/admin/accounts'),
		engine<IdentityBackupStatus>('/admin/auth-backups').catch(() => null)
	]);
	const userHasLocal =
		locals.user.has_local_username ??
		(Boolean(locals.user.username.trim()) && !locals.user.username.trim().toLowerCase().startsWith('oidc:'));
	const accounts = result.accounts.map((account) => {
		const hasLocal =
			account.has_local_username ??
			(Boolean(account.username.trim()) && !account.username.trim().toLowerCase().startsWith('oidc:'));
		return {
			...account,
			username: hasLocal ? account.username : '',
			has_local_username: hasLocal
		};
	});
	return {
		user: {
			...locals.user,
			username: userHasLocal ? locals.user.username : '',
			has_local_username: userHasLocal
		},
		accounts,
		current_user_id: locals.user.id,
		identityBackup
	};
};

export const actions: Actions = {
	create: async ({ request }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '').trim();
		const display_name = String(form.get('display_name') || '').trim();
		const password = String(form.get('password') || '');
		const role = form.get('role') === 'admin' ? 'admin' : 'user';
		if (!username || username.length > 160 || Array.from(username).some((ch) => ch.charCodeAt(0) < 32)) {
			return validationFailure('Enter a valid username.');
		}
		if (username.toLowerCase().startsWith('oidc:')) {
			return validationFailure("Usernames cannot start with 'oidc:'.");
		}
		if (password.length < 12 || password.length > 1024) {
			return validationFailure('Use a password between 12 and 1024 characters.');
		}
		try {
			await engine('/admin/accounts', { method: 'POST', body: JSON.stringify({ username, display_name, password, role }) });
			return { message: `Profile for ${username} created.` };
		} catch (error) { return failure(error); }
	},
	setStatus: async ({ request }) => {
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '');
		const status = form.get('status') === 'active' ? 'active' : 'disabled';
		try {
			await engine(`/admin/accounts/${encodeURIComponent(account_id)}/status`, { method: 'PUT', body: JSON.stringify({ status }) });
			return { message: status === 'active' ? 'Account enabled.' : 'Account disabled.' };
		} catch (error) { return failure(error); }
	},
	resetPassword: async ({ request }) => {
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '');
		const password = String(form.get('password') || '');
		if (password.length < 12 || password.length > 1024) {
			return validationFailure('Use a password between 12 and 1024 characters.');
		}
		try {
			await engine(`/admin/accounts/${encodeURIComponent(account_id)}/password`, { method: 'POST', body: JSON.stringify({ password }) });
			return { message: 'Password reset; previous sessions were signed out.' };
		} catch (error) { return failure(error); }
	}
};
