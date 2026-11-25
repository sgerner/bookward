import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '#lib/server/engine.js';

type Account = { id: string; profile_id: string; username: string; display_name: string; role: 'admin' | 'user'; status: 'active' | 'disabled' };
type IdentityBackupStatus = {
	enabled: boolean;
	interval_hours: number;
	last_success_at: string | null;
	last_attempt_at: string | null;
	last_error: string | null;
	last_restore_at: string | null;
	backups: Array<{ id: string; created_at: string; size_bytes: number }>;
};
const hasControlCharacters = (value: string) =>
	[...value].some((character) => character.charCodeAt(0) < 32);

const failure = (
	formId: string,
	error: unknown,
	fieldErrors: Record<string, string> = {},
	values?: Record<string, string>
) =>
	fail(error instanceof EngineError ? error.status : 503, {
		formId,
		message: error instanceof Error ? error.message : 'Account operation failed.',
		error: true as const,
		fieldErrors,
		...(values ? { values } : {})
	});

export const load: PageServerLoad = async ({ locals }) => {
	if (!locals.user) throw redirect(303, '/login');
	if (locals.user.role !== 'admin') throw redirect(303, '/');
	const [result, identityBackup] = await Promise.all([
		engine<{ accounts: Account[] }>('/admin/accounts'),
		engine<IdentityBackupStatus>('/admin/auth-backups').catch(() => null)
	]);
	return { accounts: result.accounts, current_user_id: locals.user.id, identityBackup };
};

export const actions: Actions = {
	create: async ({ request }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '').trim();
		const display_name = String(form.get('display_name') || '').trim();
		const password = String(form.get('password') || '');
		const rawRole = String(form.get('role') || 'user');
		const role = rawRole === 'admin' ? 'admin' : 'user';
		const values = { username, display_name, role };
		const fieldErrors: Record<string, string> = {};
		if (!username || username.length > 160 || hasControlCharacters(username)) {
			fieldErrors.username = 'Enter a valid username.';
		}
		if (display_name.length > 160) {
			fieldErrors.display_name = 'Use 160 characters or fewer.';
		}
		if (password.length < 12 || password.length > 1024) {
			fieldErrors.password = 'Use a password between 12 and 1024 characters.';
		}
		if (rawRole !== 'user' && rawRole !== 'admin') {
			fieldErrors.role = 'Choose a valid account role.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			return fail(400, {
				formId: 'create',
				message: Object.values(fieldErrors)[0]!,
				error: true as const,
				fieldErrors,
				values
			});
		}
		try {
			await engine('/admin/accounts', { method: 'POST', body: JSON.stringify({ username, display_name, password, role }) });
			return { formId: 'create', message: `Profile for ${username} created.` };
		} catch (error) {
			const status = error instanceof EngineError ? error.status : 503;
			const msg = error instanceof Error ? error.message : 'Account operation failed.';
			const mappedFieldErrors: Record<string, string> =
				status === 409 || (status === 400 && /username/i.test(msg))
					? { username: msg }
					: status === 400 && /password/i.test(msg)
						? { password: msg }
						: status === 400 && /role/i.test(msg)
							? { role: msg }
							: {};
			return failure('create', error, mappedFieldErrors, values);
		}
	},
	setStatus: async ({ request }) => {
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '').trim();
		const status = form.get('status') === 'active' ? 'active' : 'disabled';
		const formId = account_id ? `setStatus-${account_id}` : 'setStatus';
		if (!account_id) {
			return fail(400, {
				formId,
				message: 'Choose a valid account.',
				error: true as const,
				fieldErrors: {}
			});
		}
		try {
			await engine(`/admin/accounts/${encodeURIComponent(account_id)}/status`, { method: 'PUT', body: JSON.stringify({ status }) });
			return { formId, message: status === 'active' ? 'Account enabled.' : 'Account disabled.' };
		} catch (error) { return failure(formId, error); }
	},
	resetPassword: async ({ request }) => {
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '').trim();
		const password = String(form.get('password') || '');
		const formId = account_id ? `resetPassword-${account_id}` : 'resetPassword';
		if (!account_id) {
			return fail(400, {
				formId,
				message: 'Choose a valid account.',
				error: true as const,
				fieldErrors: {}
			});
		}
		if (password.length < 12 || password.length > 1024) {
			return fail(400, {
				formId,
				message: 'Use a password between 12 and 1024 characters.',
				error: true as const,
				fieldErrors: { password: 'Use a password between 12 and 1024 characters.' }
			});
		}
		try {
			await engine(`/admin/accounts/${encodeURIComponent(account_id)}/password`, { method: 'POST', body: JSON.stringify({ password }) });
			return { formId, message: 'Password reset; previous sessions were signed out.' };
		} catch (error) {
			const msg = error instanceof Error ? error.message : 'Account operation failed.';
			const mappedFieldErrors: Record<string, string> =
				error instanceof EngineError && error.status === 400 && /password/i.test(msg)
					? { password: msg }
					: {};
			return failure(formId, error, mappedFieldErrors);
		}
	}
};
