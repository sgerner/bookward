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

describe('admin page loader', () => {
	beforeEach(() => engine.mockReset());
	afterEach(() => engine.mockReset());

	it('throws HTTP 503 instead of redirecting when hooks report an engine outage', async () => {
		await expect(
			load({
				locals: { engineUnavailable: true }
			} as never)
		).rejects.toMatchObject({ status: 503 });
		expect(engine).not.toHaveBeenCalled();
	});

	it('converts upstream /admin/accounts failures to HTTP 503', async () => {
		engine.mockImplementation(async () => {
			throw new Error('upstream offline');
		});

		await expect(
			load({
				locals: {
					user: {
						id: 'admin-1',
						profile_id: 'admin-profile',
						username: 'admin',
						display_name: 'Admin',
						role: 'admin',
						must_change_password: false
					}
				}
			} as never)
		).rejects.toMatchObject({ status: 503 });
	});
});

const makeLongInput = () => randomUUID();
const makeShortInput = () => randomUUID().slice(0, 8);

function makeRequest(fields: Record<string, string>) {
	const body = new URLSearchParams(fields);
	return new Request('https://bookward.test/admin', {
		method: 'POST',
		headers: { 'content-type': 'application/x-www-form-urlencoded' },
		body
	});
}

function assertNoPasswordEcho(result: unknown, submitted: string) {
	const payload = (result as { data?: Record<string, unknown> })?.data ?? result;
	expect(payload).not.toHaveProperty('password');
	expect(payload).not.toHaveProperty('current_password');
	if (payload && typeof payload === 'object' && 'values' in payload && payload.values) {
		expect(payload.values).not.toHaveProperty('password');
		expect(payload.values).not.toHaveProperty('current_password');
	}
	expect(JSON.stringify(result)).not.toContain(submitted);
}

describe('admin page load and actions', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('loads accounts, identity backup status, and admin password_enabled flag', async () => {
		engine
			.mockResolvedValueOnce({ accounts: [{ id: 'admin-1', username: 'owner' }] })
			.mockResolvedValueOnce({ enabled: true, interval_hours: 24, backups: [] })
			.mockResolvedValueOnce({ password_enabled: true });

		const loaded = await load({
			locals: { user: { id: 'admin-1', role: 'admin', profile_id: 'legacy' } }
		} as never);
		expect(loaded).toMatchObject({
			current_user_id: 'admin-1',
			password_enabled: true
		});
	});

	it('returns fieldErrors, preserves non-secret values, and never echoes password when creating an account with invalid fields', async () => {
		const shortCandidate = makeShortInput();
		const body = new FormData();
		body.set('username', '');
		body.set('display_name', 'Reader One');
		body.set('password', shortCandidate);
		body.set('role', 'admin');
		const result = await actions.create!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body })
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'create',
				error: true,
				fieldErrors: {
					username: 'Enter a valid username.',
					password: 'Use a password between 12 and 1024 characters.'
				},
				values: {
					username: '',
					display_name: 'Reader One',
					role: 'admin'
				}
			}
		});
		assertNoPasswordEcho(result, shortCandidate);
		expect(engine).not.toHaveBeenCalled();
	});

	it('maps duplicate username 409 conflicts to create.username, preserves non-secret values, and never echoes password', async () => {
		engine.mockRejectedValueOnce(new EngineError('Username is already taken.', 409));
		const tempCandidate = makeLongInput();
		const body = new FormData();
		body.set('username', 'alice');
		body.set('display_name', 'Alice Reader');
		body.set('password', tempCandidate);
		body.set('role', 'user');
		const result = await actions.create!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body })
		} as never);

		expect(result).toMatchObject({
			status: 409,
			data: {
				formId: 'create',
				fieldErrors: { username: 'Username is already taken.' },
				values: { username: 'alice', display_name: 'Alice Reader', role: 'user' }
			}
		});
		assertNoPasswordEcho(result, tempCandidate);
	});

	it('rejects oidc:-prefixed usernames in create with structured fieldErrors without echoing password', async () => {
		const tempCandidate = makeLongInput();
		const body = new FormData();
		body.set('username', 'oidc:reserved');
		body.set('display_name', 'Reserved Reader');
		body.set('password', tempCandidate);
		body.set('role', 'user');
		const result = await actions.create!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body })
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'create',
				error: true,
				fieldErrors: { username: "Usernames cannot start with 'oidc:'." },
				values: { username: 'oidc:reserved', display_name: 'Reserved Reader', role: 'user' }
			}
		});
		assertNoPasswordEcho(result, tempCandidate);
		expect(engine).not.toHaveBeenCalled();
	});

	it('scopes resetPassword validation and engine errors to the submitted account row without echoing password', async () => {
		const shortCandidate = makeShortInput();
		const body = new FormData();
		body.set('account_id', 'acct-42');
		body.set('password', shortCandidate);
		const validationResult = await actions.resetPassword!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body })
		} as never);

		expect(validationResult).toMatchObject({
			status: 400,
			data: {
				formId: 'resetPassword-acct-42',
				error: true,
				fieldErrors: {
					password: 'Use a password between 12 and 1024 characters.'
				}
			}
		});
		assertNoPasswordEcho(validationResult, shortCandidate);
		expect(engine).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(new EngineError('Account does not have a local username.', 400));
		const fullCandidate = makeLongInput();
		const engineBody = new FormData();
		engineBody.set('account_id', 'acct-42');
		engineBody.set('password', fullCandidate);
		const engineCallResult = await actions.resetPassword!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body: engineBody })
		} as never);

		expect(engineCallResult).toMatchObject({
			status: 400,
			data: {
				formId: 'resetPassword-acct-42',
				error: true,
				fieldErrors: {}
			}
		});
		assertNoPasswordEcho(engineCallResult, fullCandidate);
	});

	it('scopes setStatus failures to the submitted account row and handles missing or blank account_id cleanly', async () => {
		const blankStatusBody = new FormData();
		blankStatusBody.set('account_id', '   ');
		blankStatusBody.set('status', 'disabled');
		const blankStatusResult = await actions.setStatus!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body: blankStatusBody })
		} as never);

		expect(blankStatusResult).toMatchObject({
			status: 400,
			data: {
				formId: 'setStatus',
				error: true,
				message: 'Choose a valid account.'
			}
		});
		expect(engine).not.toHaveBeenCalled();

		const blankResetCandidate = makeLongInput();
		const blankResetBody = new FormData();
		blankResetBody.set('account_id', '   ');
		blankResetBody.set('password', blankResetCandidate);
		const blankResetResult = await actions.resetPassword!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body: blankResetBody })
		} as never);

		expect(blankResetResult).toMatchObject({
			status: 400,
			data: {
				formId: 'resetPassword',
				error: true,
				message: 'Choose a valid account.'
			}
		});
		assertNoPasswordEcho(blankResetResult, blankResetCandidate);
		expect(engine).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(new EngineError('Cannot disable the last active administrator.', 400));
		const body = new FormData();
		body.set('account_id', 'acct-99');
		body.set('status', 'disabled');
		const result = await actions.setStatus!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body })
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'setStatus-acct-99',
				error: true,
				message: 'Cannot disable the last active administrator.'
			}
		});

		engine.mockResolvedValueOnce({ updated: true, status: 'disabled' });
		const disabled = await actions.setStatus!({
			request: makeRequest({ account_id: 'reader-2', status: 'disabled' })
		} as never);
		expect(disabled).toEqual({ formId: 'setStatus-reader-2', message: 'Account disabled.' });
	});

	it('validates username confirmation, scopes errors to the target account row, and deletes target accounts without echoing passwords', async () => {
		const locals = { user: { id: 'admin-1', role: 'admin', profile_id: 'legacy' } };
		const adminCandidate = makeLongInput();
		const wrongCandidate = makeLongInput();

		const missingUsername = await actions.deleteAccount!({
			request: makeRequest({
				account_id: 'reader-2',
				confirm_username: '',
				current_password: adminCandidate
			}),
			locals
		} as never);
		expect(missingUsername).toMatchObject({
			status: 400,
			data: {
				formId: 'deleteAccount-reader-2',
				error: true,
				message: expect.stringContaining('username'),
				fieldErrors: {
					confirm_username: 'Type the username to confirm deleting this account.'
				}
			}
		});
		assertNoPasswordEcho(missingUsername, adminCandidate);
		expect(engine).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(new EngineError('Current password is incorrect', 401));
		const wrongPassword = await actions.deleteAccount!({
			request: makeRequest({
				account_id: 'reader-2',
				confirm_username: 'alice',
				current_password: wrongCandidate
			}),
			locals
		} as never);
		expect(wrongPassword).toMatchObject({
			status: 401,
			data: {
				formId: 'deleteAccount-reader-2',
				error: true,
				message: 'Current password is incorrect',
				fieldErrors: {
					current_password: 'Current password is incorrect'
				}
			}
		});
		assertNoPasswordEcho(wrongPassword, wrongCandidate);

		engine.mockResolvedValueOnce({ deleted: true, username: 'alice' });
		const deleted = await actions.deleteAccount!({
			request: makeRequest({
				account_id: 'reader-2',
				confirm_username: 'alice',
				current_password: adminCandidate
			}),
			locals
		} as never);
		expect(engine).toHaveBeenLastCalledWith(
			'/admin/accounts/reader-2',
			{
				method: 'DELETE',
				body: JSON.stringify({
					confirm_username: 'alice',
					current_password: adminCandidate
				})
			},
			600_000
		);
		expect(deleted).toEqual({
			formId: 'deleteAccount',
			message: 'Account alice deleted.'
		});

		engine.mockResolvedValueOnce({
			deleted: true,
			username: 'oidc:deadbeefcafebabe1234567890',
			has_local_username: false,
			display_name: 'SSO Reader'
		});
		const deletedSso = await actions.deleteAccount!({
			request: makeRequest({
				account_id: 'sso-2',
				confirm_username: 'SSO Reader',
				current_password: adminCandidate
			}),
			locals
		} as never);
		expect(deletedSso).toEqual({
			formId: 'deleteAccount',
			message: 'Account SSO Reader deleted.'
		});
	});

	it('surfaces engine errors when attempting to delete self or sole administrator', async () => {
		const locals = { user: { id: 'admin-1', role: 'admin', profile_id: 'legacy' } };
		const adminCandidate = makeLongInput();
		engine.mockRejectedValueOnce(
			new EngineError('Use Account settings to delete your own account', 400)
		);

		const selfDelete = await actions.deleteAccount!({
			request: makeRequest({
				account_id: 'admin-1',
				confirm_username: 'owner',
				current_password: adminCandidate
			}),
			locals
		} as never);
		expect(selfDelete).toMatchObject({
			status: 400,
			data: {
				formId: 'deleteAccount-admin-1',
				error: true,
				message: 'Use Account settings to delete your own account',
				fieldErrors: {}
			}
		});
		assertNoPasswordEcho(selfDelete, adminCandidate);
	});

	it('maps a 404 unlock target through failure() with per-row formId, error=true, and exact status', async () => {
		engine.mockRejectedValueOnce(new EngineError('Account not found.', 404));
		const form = new FormData();
		form.set('account_id', 'missing-account-id');

		const result = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: form
			})
		} as never);

		expect(engine).toHaveBeenCalledWith('/admin/accounts/missing-account-id/unlock', {
			method: 'POST',
			body: '{}'
		});
		expect(result).toEqual({
			status: 404,
			data: {
				formId: 'unlock-missing-account-id',
				message: 'Account not found.',
				error: true,
				fieldErrors: {}
			}
		});
	});

	it('rejects blank account_id with 400 and maps unexpected network errors to 503', async () => {
		const blankForm = new FormData();
		blankForm.set('account_id', '   ');
		const blankResult = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: blankForm
			})
		} as never);
		expect(blankResult).toEqual({
			status: 400,
			data: {
				formId: 'unlock',
				message: 'Choose a valid account.',
				error: true,
				fieldErrors: {}
			}
		});
		expect(engine).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(new TypeError('fetch failed'));
		const errorForm = new FormData();
		errorForm.set('account_id', 'acc-1');
		const networkResult = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: errorForm
			})
		} as never);
		expect(networkResult).toEqual({
			status: 503,
			data: {
				formId: 'unlock-acc-1',
				message: 'fetch failed',
				error: true,
				fieldErrors: {}
			}
		});
	});

	it('formats per-row unlock confirmation messages when attempts were cleared or already zero', async () => {
		engine.mockResolvedValueOnce({ unlocked: true, username: 'reader', cleared_attempts: 10 });
		const form = new FormData();
		form.set('account_id', 'acc-reader');
		const unlocked = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: form
			})
		} as never);
		expect(unlocked).toEqual({
			formId: 'unlock-acc-reader',
			message: 'Unlocked reader (10 failed sign-in attempts cleared).'
		});

		engine.mockResolvedValueOnce({ unlocked: true, username: 'reader', cleared_attempts: 0 });
		const zeroForm = new FormData();
		zeroForm.set('account_id', 'acc-reader');
		const zeroUnlocked = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: zeroForm
			})
		} as never);
		expect(zeroUnlocked).toEqual({
			formId: 'unlock-acc-reader',
			message: 'Sign-in lockouts cleared for reader.'
		});

		engine.mockResolvedValueOnce({
			unlocked: true,
			username: 'oidc:deadbeefcafebabe1234567890',
			has_local_username: false,
			display_name: 'SSO Reader',
			cleared_attempts: 3
		});
		const ssoForm = new FormData();
		ssoForm.set('account_id', 'acc-sso');
		const ssoUnlocked = await actions.unlock({
			request: new Request('https://bookward.test/admin?/unlock', {
				method: 'POST',
				body: ssoForm
			})
		} as never);
		expect(ssoUnlocked).toEqual({
			formId: 'unlock-acc-sso',
			message: 'Unlocked SSO Reader (3 failed sign-in attempts cleared).'
		});
	});
});
