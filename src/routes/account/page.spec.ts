import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import AccountPage from './+page.svelte';

describe('account page SSR', () => {
	it('renders shared header with sign-out and ThemePicker during forced password reset', () => {
		const { body } = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'reader-1',
						profile_id: 'profile-1',
						username: 'reader',
						has_local_username: true,
						display_name: 'Temp Reader',
						role: 'user',
						must_change_password: true,
					},
					methods: {
						password_enabled: true,
						has_local_username: true,
						oidc_identities: [],
					},
					oidcEnabled: false,
					forcePasswordChange: true,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false,
				},
				form: null,
			},
		});

		expect(body).toContain('Temp Reader');
		expect(body).toContain('aria-label="Choose appearance"');
		expect(body).toContain('action="/auth/logout"');
		expect(body).toContain('Set a personal password before continuing.');
		expect(body).toContain('12 to 1024 characters');
	});

	it('prompts SSO-only accounts for a username and disables disconnecting the sole SSO login', () => {
		const { body } = render(AccountPage, {
			props: {
				data: {
					user: {
						id: 'sso-1',
						profile_id: 'sso-profile',
						username: 'oidc:9999aaaa8888bbbb',
						has_local_username: false,
						display_name: 'SSO Reader',
						role: 'user',
						must_change_password: false,
					},
					methods: {
						password_enabled: false,
						has_local_username: false,
						oidc_identities: [
							{
								id: 7,
								issuer: 'https://idp.example',
								email: 'sso@example.test',
								created_at: '2026-10-01T00:00:00Z',
							},
						],
					},
					oidcEnabled: true,
					forcePasswordChange: false,
					reauthenticationRequired: false,
					identityLinked: false,
					linkError: false,
				},
				form: null,
			},
		});

		expect(body).toContain('name="username"');
		expect(body).not.toContain('name="current_password"');
		expect(body).not.toContain('oidc:9999aaaa8888bbbb');
		expect(body).toContain('disabled');
		expect(body).not.toContain('action="?/unlink"');
	});
});
