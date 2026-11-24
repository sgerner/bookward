import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import AccountPage from './+page.svelte';

describe('account page SSR', () => {
	it('requires SSO confirmation before rendering the local credential enrollment form and disables disconnecting the sole SSO login', () => {
		const { body } = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'sso-1',
						profile_id: 'sso-profile',
						username: '',
						has_local_username: false,
						display_name: 'SSO Reader',
						role: 'user',
						must_change_password: false
					},
					methods: {
						password_enabled: false,
						has_local_username: false,
						enrollment_verified: false,
						oidc_identities: [
							{
								id: 7,
								issuer: 'https://idp.example',
								email: 'sso@example.test',
								created_at: '2026-10-01T00:00:00Z'
							}
						]
					},
					oidcEnabled: true,
					enrollmentVerified: false,
					forcePasswordChange: false,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false,
					enrollConfirmed: false,
					enrollError: false
				},
				form: null
			}
		});

		expect(body).toContain('/auth/oidc/start?intent=enroll&amp;profile_id=sso-profile');
		expect(body).toContain('Confirm with SSO');
		expect(body).not.toContain('name="username"');
		expect(body).not.toContain('name="new_password"');
		expect(body).toContain('disabled');
		expect(body).not.toContain('action="?/unlink"');
	});

	it('renders the inline username + password enrollment form once SSO confirmation succeeds', () => {
		const { body } = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'sso-1',
						profile_id: 'sso-profile',
						username: '',
						has_local_username: false,
						display_name: 'SSO Reader',
						role: 'user',
						must_change_password: false
					},
					methods: {
						password_enabled: false,
						has_local_username: false,
						enrollment_verified: true,
						oidc_identities: [
							{
								id: 7,
								issuer: 'https://idp.example',
								email: 'sso@example.test',
								created_at: '2026-10-01T00:00:00Z'
							}
						]
					},
					oidcEnabled: true,
					enrollmentVerified: true,
					forcePasswordChange: false,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false,
					enrollConfirmed: true,
					enrollError: false
				},
				form: null
			}
		});

		expect(body).toContain('Single sign-on confirmed.');
		expect(body).toContain('name="username"');
		expect(body).toContain('name="new_password"');
		expect(body).not.toContain('name="current_password"');
		expect(body).not.toContain('Confirm with SSO');
	});

	it('renders standard password change and allows SSO disconnect for legacy oidc:-prefixed local accounts', () => {
		const { body } = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'legacy-1',
						profile_id: 'legacy-profile',
						username: 'oidc:legacy-reader',
						has_local_username: true,
						display_name: '',
						role: 'user',
						must_change_password: false
					},
					methods: {
						password_enabled: true,
						has_local_username: true,
						enrollment_verified: false,
						oidc_identities: [
							{
								id: 9,
								issuer: 'https://idp.example',
								email: 'legacy@example.test',
								created_at: '2026-10-01T00:00:00Z'
							}
						]
					},
					oidcEnabled: true,
					enrollmentVerified: false,
					forcePasswordChange: false,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false,
					enrollConfirmed: false,
					enrollError: false
				},
				form: null
			}
		});

		expect(body).toContain('oidc:legacy-reader');
		expect(body).toContain('name="current_password"');
		expect(body).not.toContain('name="username"');
		expect(body).toContain('action="?/unlink"');
	});
});
