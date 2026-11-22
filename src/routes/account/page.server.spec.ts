import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn(),
}));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		constructor(
			message: string,
			readonly status: number
		) {
			super(message);
		}
	},
	engine,
}));

import { actions, load } from './+page.server';

describe('account page server', () => {
	beforeEach(() => engine.mockReset());

	it('derives forcePasswordChange from locals.user.must_change_password and clears stale query flags', async () => {
		engine
			.mockResolvedValueOnce({ password_enabled: true, has_local_username: true, oidc_identities: [] })
			.mockResolvedValueOnce({ oidc_enabled: false });

		const forced = await load({
			locals: {
				user: {
					id: 'reader-1',
					profile_id: 'profile-1',
					username: 'reader',
					has_local_username: true,
					display_name: 'Reader',
					role: 'user',
					must_change_password: true,
				},
			},
			url: new URL('https://bookward.test/account'),
		} as never);

		expect(forced).toMatchObject({ forcePasswordChange: true });

		await expect(
			load({
				locals: {
					user: {
						id: 'reader-1',
						profile_id: 'profile-1',
						username: 'reader',
						has_local_username: true,
						display_name: 'Reader',
						role: 'user',
						must_change_password: false,
					},
				},
				url: new URL('https://bookward.test/account?force_password_change=1'),
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/account' });
	});

	it('redirects away from ?force_password_change=1 into the workspace after completing a forced password update', async () => {
		engine.mockResolvedValueOnce({ session_token: 'rotated-session', max_age: 86400 });
		const cookies = { set: vi.fn() };
		const form = new URLSearchParams({
			current_password: 'temporary-password-123',
			new_password: 'personal-password-456',
		});
		const request = new Request('https://bookward.test/account?/changePassword', {
			method: 'POST',
			headers: { 'content-type': 'application/x-www-form-urlencoded' },
			body: form,
		});

		await expect(
			actions.changePassword({
				request,
				cookies,
				url: new URL(request.url),
				locals: {
					user: {
						id: 'reader-1',
						profile_id: 'profile-1',
						username: 'reader',
						has_local_username: true,
						display_name: 'Reader',
						role: 'user',
						must_change_password: true,
					},
				},
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/' });

		expect(cookies.set).toHaveBeenCalledWith(
			'bookward_session',
			'rotated-session',
			expect.objectContaining({ httpOnly: true, path: '/', maxAge: 86400 })
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
				must_change_password: false,
			},
		};

		const missingUsername = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ new_password: 'personal-password-456' }),
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals,
		} as never);
		expect(missingUsername).toMatchObject({ status: 400 });

		const reservedUsername = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ username: 'oidc:custom', new_password: 'personal-password-456' }),
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals,
		} as never);
		expect(reservedUsername).toMatchObject({ status: 400 });

		engine.mockResolvedValueOnce({ session_token: 'enrolled-session', max_age: 86400 });
		const enrolled = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				headers: { 'content-type': 'application/x-www-form-urlencoded' },
				body: new URLSearchParams({ username: 'chosen-reader', new_password: 'personal-password-456' }),
			}),
			cookies,
			url: new URL('https://bookward.test/account?/changePassword'),
			locals: ssoLocals,
		} as never);

		expect(enrolled).toEqual({ message: 'Password updated.' });
		expect(engine).toHaveBeenCalledWith(
			'/auth/password/change',
			expect.objectContaining({
				method: 'POST',
				body: JSON.stringify({
					username: 'chosen-reader',
					current_password: '',
					new_password: 'personal-password-456',
				}),
			})
		);
	});
});
