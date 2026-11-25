import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import LoginPage from '../routes/login/+page.svelte';
import AccountPage from '../routes/account/+page.svelte';
import AdminPage from '../routes/admin/+page.svelte';

function duplicateIds(html: string): string[] {
	const seen = new Set<string>();
	const duplicates = new Set<string>();
	for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
		const id = match[1]!;
		if (seen.has(id)) duplicates.add(id);
		seen.add(id);
	}
	return [...duplicates];
}

const baseLoginData = {
	setup_required: false,
	local_login_enabled: true,
	oidc_enabled: false,
	next: '/',
	auto_sso: false,
	manual_login: false,
	error: ''
};

describe('auth form accessibility SSR markup', () => {
	it('renders /login setup password rule outside the label and binds field errors with aria-invalid', () => {
		const html = render(LoginPage, {
			props: {
				data: { ...baseLoginData, setup_required: true },
				form: {
					formId: 'setup',
					error: true,
					message: 'Use a password between 12 and 1024 characters.',
					fieldErrors: { password: 'Use a password between 12 and 1024 characters.' },
					values: { username: 'admin' }
				}
			} as never
		}).body;

		expect(duplicateIds(html)).toEqual([]);
		expect(html).not.toMatch(/<label[^>]*>[\s\S]*12 characters[\s\S]*<\/label>/);
		expect(html).toContain('id="setup-password-hint"');
		expect(html).toContain('id="setup-password-error"');
		expect(html).toContain('aria-describedby="setup-password-error setup-password-hint"');
		expect(html).toContain('aria-invalid="true"');
		expect(html).toContain('value="admin"');
	});

	it('renders a shared #login-error-summary and marks both username and password aria-invalid on 401 credential failures', () => {
		const html = render(LoginPage, {
			props: {
				data: baseLoginData,
				form: {
					formId: 'login',
					error: true,
					credentialError: true,
					message: 'Username or password is incorrect.',
					fieldErrors: {},
					values: { username: 'reader' }
				}
			} as never
		}).body;

		expect(duplicateIds(html)).toEqual([]);
		expect(html).toContain('id="login-error-summary"');
		expect(html).toContain('Username or password is incorrect.');
		expect(html).toMatch(
			/id="login-username"[^>]*aria-describedby="login-error-summary"[^>]*aria-invalid="true"/
		);
		expect(html).toMatch(
			/id="login-password"[^>]*aria-describedby="login-error-summary"[^>]*aria-invalid="true"/
		);
		expect(html).not.toContain('id="login-password-error"');
	});

	it('renders /account 12-character password hint and normalized unlink banners', () => {
		const baseAccountData = {
			user: {
				id: 'u-1',
				profile_id: 'prof-1',
				username: 'alice',
				display_name: 'Alice',
				role: 'user',
				must_change_password: false
			},
			methods: {
				password_enabled: true,
				oidc_identities: [
					{ id: 1, issuer: 'https://sso-a.example', email: 'alice@a.example', created_at: '' },
					{ id: 2, issuer: 'https://sso-b.example', email: 'alice@b.example', created_at: '' }
				]
			},
			oidcEnabled: true,
			forcePasswordChange: false,
			reauthenticationRequired: false,
			identityLinked: false,
			linkError: false
		};

		const html = render(AccountPage, {
			props: {
				data: baseAccountData,
				form: {
					formId: 'changePassword',
					error: true,
					message: 'Use a password between 12 and 1024 characters.',
					fieldErrors: { new_password: 'Use a password between 12 and 1024 characters.' }
				}
			} as never
		}).body;

		expect(duplicateIds(html)).toEqual([]);
		expect(html).not.toMatch(/<label[^>]*>[\s\S]*12 characters[\s\S]*<\/label>/);
		expect(html).toContain('aria-describedby="account-new-password-error account-new-password-hint"');
		expect(html).not.toMatch(/id="account-current-password"[^>]*aria-invalid="true"/);

		const unlinkErrorHtml = render(AccountPage, {
			props: {
				data: baseAccountData,
				form: {
					formId: 'unlink',
					error: true,
					message: 'Add a password before removing the last sign-in method.',
					fieldErrors: {}
				}
			} as never
		}).body;
		expect(unlinkErrorHtml).toContain('Add a password before removing the last sign-in method.');
		expect((unlinkErrorHtml.match(/role="alert"/g) ?? []).length).toBe(1);
	});

	it('scopes /admin resetPassword errors to the submitted account without duplicate IDs or unrelated alerts', () => {
		const html = render(AdminPage, {
			props: {
				data: {
					current_user_id: 'admin-1',
					identityBackup: null,
					accounts: [
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
							status: 'active'
						},
						{
							id: 'user-3',
							profile_id: 'prof-3',
							username: 'bob',
							display_name: 'Bob',
							role: 'user',
							status: 'active'
						}
					]
				},
				form: {
					formId: 'resetPassword-user-2',
					error: true,
					message: 'Use a password between 12 and 1024 characters.',
					fieldErrors: { password: 'Use a password between 12 and 1024 characters.' }
				}
			} as never
		}).body;

		expect(duplicateIds(html)).toEqual([]);
		expect(html).toContain(
			'id="reset-password-user-2" aria-describedby="reset-password-user-2-error reset-password-user-2-hint" aria-invalid="true"'
		);
		expect(html).toContain('id="reset-password-user-3" aria-describedby="reset-password-user-3-hint"');
		expect(html).not.toContain('reset-password-user-3-error');
		expect((html.match(/role="alert"/g) ?? []).length).toBe(0);

		const statusAlertHtml = render(AdminPage, {
			props: {
				data: {
					current_user_id: 'admin-1',
					identityBackup: null,
					accounts: [
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
							status: 'active'
						},
						{
							id: 'user-3',
							profile_id: 'prof-3',
							username: 'bob',
							display_name: 'Bob',
							role: 'user',
							status: 'active'
						}
					]
				},
				form: {
					formId: 'setStatus-user-2',
					error: true,
					message: 'Cannot disable account.',
					fieldErrors: {}
				}
			} as never
		}).body;
		expect((statusAlertHtml.match(/role="alert"/g) ?? []).length).toBe(1);

		const missingAccountHtml = render(AdminPage, {
			props: {
				data: {
					current_user_id: 'admin-1',
					identityBackup: null,
					accounts: [
						{
							id: 'admin-1',
							profile_id: 'prof-admin',
							username: 'admin',
							display_name: 'Admin',
							role: 'admin',
							status: 'active'
						}
					]
				},
				form: {
					formId: 'setStatus',
					error: true,
					message: 'Choose a valid account.',
					fieldErrors: {}
				}
			} as never
		}).body;
		expect(missingAccountHtml).toContain('Choose a valid account.');
		expect((missingAccountHtml.match(/role="alert"/g) ?? []).length).toBe(1);

		const unmatchedAccountHtml = render(AdminPage, {
			props: {
				data: {
					current_user_id: 'admin-1',
					identityBackup: null,
					accounts: [
						{
							id: 'admin-1',
							profile_id: 'prof-admin',
							username: 'admin',
							display_name: 'Admin',
							role: 'admin',
							status: 'active'
						}
					]
				},
				form: {
					formId: 'resetPassword-deleted-user',
					error: true,
					message: 'Account not found.',
					fieldErrors: {}
				}
			} as never
		}).body;
		expect(unmatchedAccountHtml).toContain('Account not found.');
		expect((unmatchedAccountHtml.match(/role="alert"/g) ?? []).length).toBe(1);
	});
});
