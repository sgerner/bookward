import { randomUUID } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn()
}));

vi.mock('#lib/server/engine.js', async (importOriginal) => {
	const actual = await importOriginal<typeof import('#lib/server/engine.js')>();
	return {
		...actual,
		engine
	};
});

import { EngineError } from '#lib/server/engine.js';
import { actions, load } from './+page.server';

describe('account page loader', () => {
	beforeEach(() => engine.mockReset());
	afterEach(() => engine.mockReset());

	it('throws HTTP 503 instead of redirecting when hooks report an engine outage', async () => {
		await expect(
			load({
				locals: { engineUnavailable: true },
				url: new URL('https://bookward.test/account')
			} as never)
		).rejects.toMatchObject({ status: 503 });
		expect(engine).not.toHaveBeenCalled();
	});

	it('converts upstream engine errors during account loading to HTTP 503', async () => {
		engine.mockImplementation(async () => {
			throw new Error('upstream offline');
		});

		await expect(
			load({
				locals: {
					user: {
						id: 'alice',
						profile_id: 'alice-profile',
						username: 'alice',
						display_name: 'Alice',
						role: 'user',
						must_change_password: false
					}
				},
				url: new URL('https://bookward.test/account')
			} as never)
		).rejects.toMatchObject({ status: 503 });
	});
});

const makeLongInput = () => randomUUID();
const makeShortInput = () => randomUUID().slice(0, 8);

function assertNoPasswordEcho(result: unknown, ...submittedValues: string[]) {
	const payload = (result as { data?: Record<string, unknown> })?.data ?? result;
	expect(payload).not.toHaveProperty('password');
	expect(payload).not.toHaveProperty('current_password');
	expect(payload).not.toHaveProperty('new_password');
	if (payload && typeof payload === 'object' && 'values' in payload && payload.values) {
		expect(payload.values).not.toHaveProperty('password');
		expect(payload.values).not.toHaveProperty('current_password');
		expect(payload.values).not.toHaveProperty('new_password');
	}
	const serialized = JSON.stringify(result);
	for (const value of submittedValues) {
		if (value) expect(serialized).not.toContain(value);
	}
}

describe('account page actions', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('returns scoped fieldErrors for missing current password and short new password without echoing passwords', async () => {
		const shortCandidate = makeShortInput();
		const body = new FormData();
		body.set('current_password', '');
		body.set('new_password', shortCandidate);
		const result = await actions.changePassword!({
			request: new Request('https://bookward.test/account', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/account')
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'changePassword',
				error: true,
				fieldErrors: {
					current_password: 'Enter your current password.',
					new_password: 'Use a password between 12 and 1024 characters.'
				}
			}
		});
		assertNoPasswordEcho(result, shortCandidate);
		expect(engine).not.toHaveBeenCalled();
	});

	it('allows setting a first local password without current_password when password_enabled is false', async () => {
		const shortCandidate = makeShortInput();
		const body = new FormData();
		body.set('new_password', shortCandidate);
		const result = await actions.changePassword!({
			request: new Request('https://bookward.test/account', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/account')
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'changePassword',
				fieldErrors: {
					new_password: 'Use a password between 12 and 1024 characters.'
				}
			}
		});
		expect((result as { data: { fieldErrors: Record<string, string> } }).data.fieldErrors).not.toHaveProperty('current_password');
		assertNoPasswordEcho(result, shortCandidate);
	});

	it('maps engine current-password rejection to current_password fieldErrors and never echoes passwords', async () => {
		engine.mockRejectedValueOnce(new EngineError('Current password is incorrect.', 401));
		const currentCandidate = makeLongInput();
		const nextCandidate = makeLongInput();
		const body = new FormData();
		body.set('current_password', currentCandidate);
		body.set('new_password', nextCandidate);
		const result = await actions.changePassword!({
			request: new Request('https://bookward.test/account', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/account')
		} as never);

		expect(result).toMatchObject({
			status: 401,
			data: {
				formId: 'changePassword',
				fieldErrors: {
					current_password: 'Current password is incorrect.'
				}
			}
		});
		assertNoPasswordEcho(result, currentCandidate, nextCandidate);
	});

	it('normalizes unlink formId to "unlink" across validation, engine-error, and success branches', async () => {
		const invalidBody = new FormData();
		invalidBody.set('id', '0');
		const invalidResult = await actions.unlink!({
			request: new Request('https://bookward.test/account', { method: 'POST', body: invalidBody })
		} as never);
		expect(invalidResult).toMatchObject({
			status: 400,
			data: {
				formId: 'unlink',
				error: true,
				message: 'Choose a valid login method.'
			}
		});

		engine.mockRejectedValueOnce(
			new EngineError('Add a password before removing the last sign-in method.', 400)
		);
		const errorBody = new FormData();
		errorBody.set('id', '7');
		const errorResult = await actions.unlink!({
			request: new Request('https://bookward.test/account', { method: 'POST', body: errorBody })
		} as never);
		expect(errorResult).toMatchObject({
			status: 400,
			data: {
				formId: 'unlink',
				error: true,
				message: 'Add a password before removing the last sign-in method.'
			}
		});

		engine.mockResolvedValueOnce({});
		const okBody = new FormData();
		okBody.set('id', '7');
		const okResult = await actions.unlink!({
			request: new Request('https://bookward.test/account', { method: 'POST', body: okBody })
		} as never);
		expect(okResult).toEqual({
			formId: 'unlink',
			message: 'SSO login disconnected.'
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
