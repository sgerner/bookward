import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import AdminPage from './+page.svelte';

describe('admin page SSR', () => {
	it('labels SSO-only accounts without oidc hashes, disables SSO-only resets, and preserves legacy oidc:-prefixed local accounts', () => {
		const { body } = render(AdminPage, {
			props: {
				data: {
					user: {
						id: 'admin-1',
						profile_id: 'legacy',
						username: 'owner',
						has_local_username: true,
						display_name: 'Owner Admin',
						role: 'admin',
						must_change_password: false
					},
					current_user_id: 'admin-1',
					accounts: [
						{
							id: 'admin-1',
							profile_id: 'legacy',
							username: 'owner',
							has_local_username: true,
							display_name: 'Owner Admin',
							role: 'admin',
							status: 'active'
						},
						{
							id: 'local-2',
							profile_id: 'profile-2',
							username: 'alice',
							has_local_username: true,
							display_name: 'Alice Local',
							role: 'user',
							status: 'active'
						},
						{
							id: 'legacy-3',
							profile_id: 'profile-3',
							username: 'oidc:legacy-reader',
							has_local_username: true,
							display_name: 'Legacy OIDC Name',
							role: 'user',
							status: 'active'
						},
						{
							id: 'sso-4',
							profile_id: 'profile-4',
							username: 'oidc:deadbeefcafebabe1234567890',
							has_local_username: false,
							display_name: 'Casey SSO',
							role: 'user',
							status: 'active'
						}
					],
					identityBackup: null
				},
				form: null
			}
		});

		expect(body).toContain('SSO-only · user · active');
		expect(body).not.toContain('oidc:deadbeefcafebabe1234567890');
		expect(body).toContain('oidc:legacy-reader · user · active');
		expect(body).toContain('aria-label="Reset password for Alice Local"');
		expect(body).toContain('aria-label="Reset password for Legacy OIDC Name"');
		expect(body).toContain('aria-label="Reset password unavailable for Casey SSO"');
	});
});
