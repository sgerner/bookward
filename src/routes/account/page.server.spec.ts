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
import {
	currentClientIp,
	currentSessionToken,
	withSessionToken
} from '#lib/server/request-context.js';
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

function makeRequest(fields: Record<string, string>, origin = 'https://bookward.test') {
	const body = new URLSearchParams(fields);
	return new Request(`${origin}/account`, {
		method: 'POST',
		headers: { 'content-type': 'application/x-www-form-urlencoded' },
		body
	});
}

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

	it('updates the active request-context session token and forwards client IP when rotating passwords', async () => {
		let seenClientIp: string | undefined;
		engine.mockImplementationOnce(async () => {
			seenClientIp = currentClientIp();
			return { session_token: 'rotated-session', max_age: 3600 };
		});
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
		const currentCandidate = makeLongInput();
		const nextCandidate = makeLongInput();
		const form = new FormData();
		form.set('current_password', currentCandidate);
		form.set('new_password', nextCandidate);

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

			expect(result).toEqual({ formId: 'changePassword', message: 'Password updated.' });
			expect(seenClientIp).toBe('198.51.100.42');
			expect(currentSessionToken()).toBe('rotated-session');
			expect(locals.user.must_change_password).toBe(false);
			assertNoPasswordEcho(result, currentCandidate, nextCandidate);
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
		const currentCandidate = makeLongInput();
		const nextCandidate = makeLongInput();
		const form = new FormData();
		form.set('current_password', currentCandidate);
		form.set('new_password', nextCandidate);

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
				formId: 'changePassword',
				error: true,
				rateLimited: true,
				retryAfterSeconds: 720,
				fieldErrors: {},
				message: expect.stringContaining('Wait 12 minutes')
			}
		});
		assertNoPasswordEcho(result, currentCandidate, nextCandidate);
	});

	it('applies Secure cookie flags from the request URL when rotating the session on password change', async () => {
		engine.mockResolvedValue({ session_token: 'rotated-session', max_age: 7200 });
		const httpCookies = { set: vi.fn() };
		const oldCandidate = makeLongInput();
		const newCandidate = makeLongInput();
		const form = new URLSearchParams({
			current_password: oldCandidate,
			new_password: newCandidate
		});

		await actions.changePassword!({
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
		await actions.changePassword!({
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

	it('requires typed RESET before calling the engine history reset endpoint', async () => {
		const readerCandidate = makeLongInput();
		const rejected = await actions.resetHistory!({
			request: makeRequest({ current_password: readerCandidate, confirm: 'reset' })
		} as never);
		expect(rejected).toMatchObject({
			status: 400,
			data: {
				formId: 'resetHistory',
				error: true,
				message: expect.stringContaining('RESET'),
				fieldErrors: {
					confirm: 'Type RESET to confirm resetting your reading history.'
				}
			}
		});
		assertNoPasswordEcho(rejected, readerCandidate);
		expect(engine).not.toHaveBeenCalled();

		engine.mockResolvedValue({ reset: true, reads_removed: 4 });
		const ok = await actions.resetHistory!({
			request: makeRequest({ current_password: readerCandidate, confirm: 'RESET' })
		} as never);
		expect(engine).toHaveBeenCalledWith(
			'/auth/account/reset-history',
			{
				method: 'POST',
				body: JSON.stringify({ current_password: readerCandidate, confirm: 'RESET' })
			},
			600_000
		);
		expect(ok).toEqual({
			formId: 'resetHistory',
			message: 'Reading history and recommendation evidence were reset.'
		});
	});

	it('requires typed DELETE, clears bookward_session with URL Secure flags, and redirects to /login?manual=1 on self-deletion', async () => {
		const cookies = { delete: vi.fn() };
		const httpsUrl = new URL('https://bookward.test/account');
		const readerCandidate = makeLongInput();
		const ownerCandidate = makeLongInput();
		const deferredCandidate = makeLongInput();
		const missingConfirm = await actions.deleteAccount!({
			request: makeRequest({ current_password: readerCandidate, confirm: 'NO' }),
			cookies,
			url: httpsUrl
		} as never);
		expect(missingConfirm).toMatchObject({
			status: 400,
			data: {
				formId: 'deleteAccount',
				error: true,
				message: expect.stringContaining('DELETE'),
				fieldErrors: {
					confirm: 'Type DELETE to confirm deleting your account.'
				}
			}
		});
		assertNoPasswordEcho(missingConfirm, readerCandidate);
		expect(cookies.delete).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(
			new EngineError('The installation must retain an active administrator', 400)
		);
		const soleAdmin = await actions.deleteAccount!({
			request: makeRequest({ current_password: ownerCandidate, confirm: 'DELETE' }),
			cookies,
			url: httpsUrl
		} as never);
		expect(soleAdmin).toMatchObject({
			status: 400,
			data: {
				formId: 'deleteAccount',
				error: true,
				message: 'The installation must retain an active administrator',
				fieldErrors: {}
			}
		});
		assertNoPasswordEcho(soleAdmin, ownerCandidate);
		expect(cookies.delete).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(
			new EngineError(
				'Account access was revoked; remaining profile cleanup will finish automatically.',
				503
			)
		);
		const deferred = await actions.deleteAccount!({
			request: makeRequest({
				current_password: deferredCandidate,
				confirm: 'DELETE'
			}),
			cookies,
			url: httpsUrl
		} as never);
		expect(deferred).toMatchObject({
			status: 503,
			data: {
				formId: 'deleteAccount',
				error: true,
				message: 'Account access was revoked; remaining profile cleanup will finish automatically.',
				fieldErrors: {}
			}
		});
		assertNoPasswordEcho(deferred, deferredCandidate);
		expect(cookies.delete).toHaveBeenCalledTimes(1);
		cookies.delete.mockClear();

		engine.mockResolvedValueOnce({ deleted: true });
		await expect(
			actions.deleteAccount!({
				request: makeRequest({ current_password: readerCandidate, confirm: 'DELETE' }),
				cookies,
				url: httpsUrl
			} as never)
		).rejects.toMatchObject({
			status: 303,
			location: '/login?manual=1'
		});
		expect(engine).toHaveBeenLastCalledWith(
			'/auth/account',
			{
				method: 'DELETE',
				body: JSON.stringify({ current_password: readerCandidate, confirm: 'DELETE' })
			},
			600_000
		);
		expect(cookies.delete).toHaveBeenCalledWith('bookward_session', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/'
		});
	});
});
