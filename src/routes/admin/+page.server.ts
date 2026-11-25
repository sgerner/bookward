import { error as httpError, fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { EngineError, engine } from '#lib/server/engine.js';
import { formatWaitDuration } from '#lib/lockout-ui.js';
import {
	currentClientIp,
	currentSessionToken,
	resolveClientAddress,
	withRequestContext
} from '#lib/server/request-context.js';

type Account = {
	id: string;
	profile_id: string;
	username: string;
	display_name: string;
	role: 'admin' | 'user';
	status: 'active' | 'disabled';
	recent_failed_attempts?: number;
	locked_bucket_count?: number;
	lockout_retry_after_seconds?: number;
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
	if (locals?.engineUnavailable) {
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
	if (!locals.user) throw redirect(303, '/login');
	if (locals.user.role !== 'admin') throw redirect(303, '/');
	let result: { accounts: Account[] };
	let identityBackup: IdentityBackupStatus | null;
	let methods: { password_enabled: boolean };
	try {
		[result, identityBackup, methods] = await Promise.all([
			engine<{ accounts: Account[] }>('/admin/accounts'),
			engine<IdentityBackupStatus>('/admin/auth-backups').catch(() => null),
			engine<{ password_enabled: boolean }>('/auth/identities').catch(() => ({
				password_enabled: true
			}))
		]);
	} catch (error) {
		if (error instanceof EngineError && error.status === 401) throw redirect(303, '/login');
		if (error instanceof EngineError && error.status === 403) throw redirect(303, '/');
		throw httpError(503, "Bookward's recommendation engine is unavailable.");
	}
	return {
		accounts: result.accounts,
		current_user_id: locals.user.id,
		identityBackup,
		password_enabled: methods.password_enabled
	};
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
	},
	unlock: async ({ request }) => {
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '').trim();
		const formId = account_id ? `unlock-${account_id}` : 'unlock';
		if (!account_id) {
			return fail(400, {
				formId,
				message: 'Choose a valid account.',
				error: true as const,
				fieldErrors: {}
			});
		}
		try {
			const result = await engine<{ unlocked: boolean; username: string; cleared_attempts: number }>(
				`/admin/accounts/${encodeURIComponent(account_id)}/unlock`,
				{ method: 'POST', body: '{}' }
			);
			return {
				formId,
				message:
					result.cleared_attempts > 0
						? `Unlocked ${result.username} (${result.cleared_attempts} failed sign-in attempt${result.cleared_attempts === 1 ? '' : 's'} cleared).`
						: `Sign-in lockouts cleared for ${result.username}.`
			};
		} catch (error) {
			return failure(formId, error);
		}
	},
	deleteAccount: async (event) => {
		const { request, locals, setHeaders } = event;
		const form = await request.formData();
		const account_id = String(form.get('account_id') || '').trim();
		const confirm_username = String(form.get('confirm_username') || '').trim();
		const hasCurrentPassword = form.has('current_password');
		const current_password = String(form.get('current_password') || '');
		const formId = account_id ? `deleteAccount-${account_id}` : 'deleteAccount';
		if (!account_id) {
			return fail(400, {
				formId,
				message: 'Choose a valid account.',
				error: true as const,
				fieldErrors: {}
			});
		}
		const fieldErrors: Record<string, string> = {};
		if (account_id !== locals.user?.id && !confirm_username) {
			fieldErrors.confirm_username = 'Type the username to confirm deleting this account.';
		}
		if (account_id !== locals.user?.id && hasCurrentPassword && !current_password) {
			fieldErrors.current_password = 'Enter your current password.';
		}
		if (Object.keys(fieldErrors).length > 0) {
			return fail(400, {
				formId,
				message: fieldErrors.confirm_username ?? fieldErrors.current_password!,
				error: true as const,
				fieldErrors
			});
		}
		const clientIp = currentClientIp() ?? resolveClientAddress(event);
		try {
			const deleted = await withRequestContext(
				{ sessionToken: currentSessionToken(), clientIp },
				() =>
					engine<{ username?: string }>(
						`/admin/accounts/${encodeURIComponent(account_id)}`,
						{
							method: 'DELETE',
							body: JSON.stringify({
								confirm_username: confirm_username || undefined,
								current_password
							})
						},
						10 * 60_000
					)
			);
			return {
				formId: 'deleteAccount',
				message: `Account ${deleted.username || confirm_username} deleted.`
			};
		} catch (error) {
			if (error instanceof EngineError && error.status === 429) {
				const retryAfterSeconds = error.retryAfterSeconds ?? 900;
				setHeaders?.({ 'retry-after': String(retryAfterSeconds) });
				return fail(429, {
					formId,
					message: `Too many password attempts. Wait ${formatWaitDuration(retryAfterSeconds)} before trying again.`,
					error: true as const,
					fieldErrors: {},
					rateLimited: true as const,
					retryAfterSeconds
				});
			}
			const status = error instanceof EngineError ? error.status : 503;
			const msg = error instanceof Error ? error.message : 'Account operation failed.';
			const mappedFieldErrors: Record<string, string> = /current password/i.test(msg)
				? { current_password: msg }
				: status === 400 && /confirm deleting this account/i.test(msg)
					? { confirm_username: msg }
					: {};
			return failure(formId, error, mappedFieldErrors);
		}
	}
};
