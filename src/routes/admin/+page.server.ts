import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '$lib/server/engine';

type Account = { id: string; profile_id: string; username: string; display_name: string; role: 'admin' | 'user'; status: 'active' | 'disabled' };
const failure = (error: unknown) => fail(error instanceof EngineError ? error.status : 503, { message: error instanceof Error ? error.message : 'Account operation failed.', error: true as const });

export const load: PageServerLoad = async ({ locals }) => {
	if (!locals.user) throw redirect(303, '/login');
	if (locals.user.role !== 'admin') throw redirect(303, '/');
	const result = await engine<{ accounts: Account[] }>('/admin/accounts');
	return { accounts: result.accounts, current_user_id: locals.user.id };
};

export const actions: Actions = {
	create: async ({ request }) => {
		const form = await request.formData();
		const username = String(form.get('username') || '').trim();
		const display_name = String(form.get('display_name') || '').trim();
		const password = String(form.get('password') || '');
		const role = form.get('role') === 'admin' ? 'admin' : 'user';
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
		try {
			await engine(`/admin/accounts/${encodeURIComponent(account_id)}/password`, { method: 'POST', body: JSON.stringify({ password }) });
			return { message: 'Password reset; previous sessions were signed out.' };
		} catch (error) { return failure(error); }
	}
};
