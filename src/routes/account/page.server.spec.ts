import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn()
}));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		constructor(
			message = 'Engine error',
			readonly status = 500
		) {
			super(message);
		}
	},
	engine
}));

import { actions, load } from './+page.server';

describe('account page server', () => {
	beforeEach(() => engine.mockReset());

	it('applies Secure cookie flags from the request URL when rotating the session on password change', async () => {
		engine.mockResolvedValue({ session_token: 'rotated-session', max_age: 7200 });
		const httpCookies = { set: vi.fn() };
		const form = new URLSearchParams({
			current_password: 'Old-valid-password-1',
			new_password: 'New-valid-password-2'
		});

		await actions.changePassword({
			request: new Request('http://127.0.0.1:3000/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: form
			}),
			cookies: httpCookies,
			url: new URL('http://127.0.0.1:3000/account')
		} as never);

		expect(httpCookies.set).toHaveBeenCalledWith('bookward_session', 'rotated-session', {
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/',
			maxAge: 7200
		});

		const httpsCookies = { set: vi.fn() };
		await actions.changePassword({
			request: new Request('https://books.example.com/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: form
			}),
			cookies: httpsCookies,
			url: new URL('https://books.example.com/account')
		} as never);

		expect(httpsCookies.set).toHaveBeenCalledWith(
			'bookward_session',
			'rotated-session',
			expect.objectContaining({ secure: true, path: '/' })
		);
	});

	it('propagates enrollment verification state for SSO-only accounts while hiding surrogate usernames', async () => {
		engine
			.mockResolvedValueOnce({
				password_enabled: false,
				has_local_username: false,
				enrollment_verified: true,
				oidc_identities: [{ id: 1, issuer: 'https://idp.example', email: 'sso@example.test', created_at: '2026-10-01T00:00:00Z' }]
			})
			.mockResolvedValueOnce({ oidc_enabled: true });

		const loaded = await load({
			locals: {
				user: {
					id: 'sso-1',
					profile_id: 'sso-profile',
					username: 'oidc:1234567890abcdef',
					has_local_username: false,
					display_name: 'SSO Reader',
					role: 'user',
					must_change_password: false
				}
			},
			url: new URL('https://bookward.test/account?enroll=1')
		} as never);

		expect(loaded).toMatchObject({
			user: { username: '', has_local_username: false },
			enrollmentVerified: true,
			enrollConfirmed: true,
			enrollError: false
		});
	});

	it('preserves legacy oidc:-prefixed local accounts when has_local_username is true', async () => {
		engine
			.mockResolvedValueOnce({
				password_enabled: true,
				has_local_username: true,
				enrollment_verified: false,
				oidc_identities: []
			})
			.mockResolvedValueOnce({ oidc_enabled: false });

		const loaded = await load({
			locals: {
				user: {
					id: 'legacy-1',
					profile_id: 'legacy-profile',
					username: 'oidc:legacy-reader',
					has_local_username: true,
					display_name: 'Legacy Reader',
					role: 'user',
					must_change_password: false
				}
			},
			url: new URL('https://bookward.test/account')
		} as never);

		expect(loaded).toMatchObject({
			user: { username: 'oidc:legacy-reader', has_local_username: true },
			enrollmentVerified: false
		});

		const cookies = { set: vi.fn() };
		engine.mockResolvedValueOnce({ session_token: 'rotated-legacy-session', max_age: 86400 });
		const updated = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({
					current_password: 'legacy-password-123',
					new_password: 'updated-password-456'
				})
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: {
				user: {
					id: 'legacy-1',
					profile_id: 'legacy-profile',
					username: 'oidc:legacy-reader',
					has_local_username: true,
					display_name: 'Legacy Reader',
					role: 'user',
					must_change_password: false
				}
			}
		} as never);

		expect(updated).toEqual({ message: 'Password updated.' });
		expect(engine).toHaveBeenLastCalledWith(
			'/auth/password/change',
			expect.objectContaining({
				method: 'POST',
				body: JSON.stringify({
					current_password: 'legacy-password-123',
					new_password: 'updated-password-456'
				})
			})
		);
	});

	it('requires a non-oidc username when an SSO-only account enrolls a local password', async () => {
		const cookies = { set: vi.fn() };
		const ssoLocals = {
			user: {
				id: 'sso-1',
				profile_id: 'sso-profile',
				username: 'oidc:1234567890abcdef',
				has_local_username: false,
				display_name: 'SSO Reader',
				role: 'user',
				must_change_password: false
			}
		};

		const missingUsername = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ new_password: 'personal-password-456' })
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals
		} as never);
		expect(missingUsername).toMatchObject({ status: 400 });

		const reservedUsername = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ username: 'oidc:custom', new_password: 'personal-password-456' })
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals
		} as never);
		expect(reservedUsername).toMatchObject({ status: 400 });

		engine.mockResolvedValueOnce({ session_token: 'enrolled-session', max_age: 86400 });
		const enrolled = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ username: 'chosen-reader', new_password: 'personal-password-456' })
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals
		} as never);

		expect(enrolled).toEqual({ message: 'Password updated.' });
		expect(engine).toHaveBeenCalledWith(
			'/auth/password/change',
			expect.objectContaining({
				method: 'POST',
				body: JSON.stringify({
					username: 'chosen-reader',
					current_password: '',
					new_password: 'personal-password-456'
				})
			})
		);
	});
});
