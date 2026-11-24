import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		status = 500;
	},
	engine
}));

import { actions } from './+page.server';

describe('account page actions', () => {
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
});
