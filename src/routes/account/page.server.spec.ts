import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn()
}));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		constructor(
			message: string,
			readonly status = 500,
			readonly retryAfterSeconds?: number
		) {
			super(message);
		}
	},
	engine
}));

import { EngineError } from '#lib/server/engine.js';
import { currentSessionToken, withSessionToken } from '#lib/server/request-context.js';
import { actions } from './+page.server';

describe('account password change action', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('updates the active request-context session token when rotating passwords', async () => {
		engine.mockResolvedValue({ session_token: 'rotated-session', max_age: 3600 });
		const cookiesSet = vi.fn();
		const locals = {
			user: {
				id: 1,
				username: 'reader',
				role: 'user',
				profile_id: 'reader',
				must_change_password: true
			}
		};
		const form = new FormData();
		form.set('current_password', 'temporary-pass-123');
		form.set('new_password', 'permanent-pass-456');

		await withSessionToken('old-session', async () => {
			const result = await actions.changePassword({
				request: new Request('https://bookward.test/account?/changePassword', {
					method: 'POST',
					body: form
				}),
				cookies: { set: cookiesSet },
				locals,
				url: new URL('https://bookward.test/account?force_password_change=1'),
				getClientAddress: () => '198.51.100.42'
			} as never);

			expect(result).toEqual({ message: 'Password updated.' });
			expect(currentSessionToken()).toBe('rotated-session');
			expect(locals.user.must_change_password).toBe(false);
		});

		expect(cookiesSet).toHaveBeenCalledWith(
			'bookward_session',
			'rotated-session',
			expect.objectContaining({ maxAge: 3600, path: '/' })
		);
	});

	it('displays remaining wait time and sets Retry-After on HTTP 429 session lockouts', async () => {
		engine.mockRejectedValue(
			new EngineError('Too many failed password attempts.', 429, 720)
		);
		const setHeaders = vi.fn();
		const form = new FormData();
		form.set('current_password', 'wrong-current-pass');
		form.set('new_password', 'valid-new-password-123');

		const result = await actions.changePassword({
			request: new Request('https://bookward.test/account?/changePassword', {
				method: 'POST',
				body: form
			}),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/account?force_password_change=1'),
			setHeaders,
			getClientAddress: () => '198.51.100.42'
		} as never);

		expect(setHeaders).toHaveBeenCalledWith({ 'retry-after': '720' });
		expect(result).toMatchObject({
			status: 429,
			data: {
				error: true,
				rateLimited: true,
				retryAfterSeconds: 720,
				message: expect.stringContaining('Wait 12 minutes')
			}
		});
	});

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
