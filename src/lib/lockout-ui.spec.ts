import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import { formatWaitDuration } from './lockout-ui.js';
import LoginPage from '../routes/login/+page.svelte';
import AccountPage from '../routes/account/+page.svelte';
import AdminPage from '../routes/admin/+page.svelte';

describe('formatWaitDuration', () => {
	it('formats sub-minute and multi-minute lockout durations cleanly', () => {
		expect(formatWaitDuration(1)).toBe('1 second');
		expect(formatWaitDuration(42.2)).toBe('43 seconds');
		expect(formatWaitDuration(60)).toBe('1 minute');
		expect(formatWaitDuration(880)).toBe('15 minutes');
		expect(formatWaitDuration(undefined)).toBe('15 minutes');
	});
});

describe('lockout and unlock SSR markup with FormField structured errors', () => {
	it('renders /login HTTP 429 tonal warning with recovery guidance without marking inputs aria-invalid', () => {
		const html = render(LoginPage, {
			props: {
				data: {
					setup_required: false,
					local_login_enabled: true,
					oidc_enabled: false,
					next: '/',
					auto_sso: false,
					manual_login: false,
					error: ''
				},
				form: {
					formId: 'login',
					error: true,
					rateLimited: true,
					retryAfterSeconds: 880,
					message: 'Too many sign-in attempts from this address. Wait 15 minutes before trying again.',
					fieldErrors: {},
					values: { username: 'reader' }
				}
			} as never
		}).body;

		expect(html).toContain('id="login-error-summary"');
		expect(html).toContain('preset-tonal-warning');
		expect(html).toContain('Wait 15 minutes before trying again.');
		expect(html).toContain('Manage accounts');
		expect(html).toContain('auth_admin unlock');
		expect(html).toContain('value="reader"');
		expect(html).not.toContain('aria-invalid="true"');
	});

	it('renders /account HTTP 429 password-change lockout banner with warning styling', () => {
		const html = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'u-1',
						profile_id: 'prof-1',
						username: 'alice',
						display_name: 'Alice',
						role: 'user',
						must_change_password: false
					},
					methods: { password_enabled: true, oidc_identities: [] },
					oidcEnabled: false,
					forcePasswordChange: false,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false
				},
				form: {
					formId: 'changePassword',
					error: true,
					rateLimited: true,
					retryAfterSeconds: 120,
					message:
						'Too many password attempts. Wait about 2 minutes before trying again, or ask an administrator to unlock this account.',
					fieldErrors: {}
				}
			} as never
		}).body;

		expect(html).toContain('preset-tonal-warning');
		expect(html).toContain('Wait about 2 minutes before trying again');
		expect(html).not.toContain('aria-invalid="true"');
	});

	it('renders /admin rate-limited badge, per-row unlock feedback, and section fallback for missing accounts', () => {
		const accounts = [
			{
				id: 'admin-1',
				profile_id: 'prof-admin',
				username: 'admin',
				display_name: 'Admin',
				role: 'admin',
				status: 'active'
			},
			{
				id: 'user-2',
				profile_id: 'prof-2',
				username: 'alice',
				display_name: 'Alice',
				role: 'user',
				status: 'active',
				locked_bucket_count: 1,
				lockout_retry_after_seconds: 600,
				recent_failed_attempts: 5
			}
		];

		const rowHtml = render(AdminPage, {
			props: {
				data: { current_user_id: 'admin-1', identityBackup: null, accounts },
				form: {
					formId: 'unlock-user-2',
					message: 'Sign-in rate limits cleared.'
				}
			} as never
		}).body;

		expect(rowHtml).toContain('Rate-limited (1 source');
		expect(rowHtml).toContain('10 minutes left)');
		expect(rowHtml).toContain('5 recent failed sign-in attempts');
		expect(rowHtml).toContain('action="?/unlock"');
		expect(rowHtml).toContain('Sign-in rate limits cleared.');
		expect((rowHtml.match(/role="alert"/g) ?? []).length).toBe(0);

		const missingUnlockHtml = render(AdminPage, {
			props: {
				data: { current_user_id: 'admin-1', identityBackup: null, accounts: [accounts[0]!] },
				form: {
					formId: 'unlock-deleted-user',
					error: true,
					message: 'Account not found.',
					fieldErrors: {}
				}
			} as never
		}).body;

		expect(missingUnlockHtml).toContain('Account not found.');
		expect((missingUnlockHtml.match(/role="alert"/g) ?? []).length).toBe(1);
	});
});
