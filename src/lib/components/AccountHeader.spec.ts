import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import AccountHeader from './AccountHeader.svelte';

describe('AccountHeader', () => {
	it('renders active display name, ThemePicker, and profile-bound logout form including during forced reset', () => {
		const { body } = render(AccountHeader, {
			props: {
				user: {
					profile_id: 'reader-profile-1',
					username: 'reader',
					has_local_username: true,
					display_name: 'Reader One',
					role: 'user',
					must_change_password: true,
				},
			},
		});

		expect(body).toContain('Reader One');
		expect(body).toContain('aria-label="Choose appearance"');
		expect(body).toContain('action="/auth/logout"');
		expect(body).toContain('name="__profile_id"');
		expect(body).toContain('value="reader-profile-1"');
		expect(body).toContain('Sign out');
	});

	it('avoids rendering raw oidc: surrogate usernames when display_name is blank', () => {
		const { body } = render(AccountHeader, {
			props: {
				user: {
					profile_id: 'sso-profile',
					username: 'oidc:abcdef0123456789',
					has_local_username: false,
					display_name: '',
					role: 'user',
					must_change_password: false,
				},
			},
		});

		expect(body).toContain('SSO user');
		expect(body).not.toContain('oidc:abcdef0123456789');
	});
});
