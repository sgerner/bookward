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

function assertNoPasswordEcho(result: unknown, submitted: string) {
	const payload = (result as { data?: { values?: Record<string, unknown> } })?.data ?? result;
	expect(payload).not.toHaveProperty('password');
	if (payload && typeof payload === 'object' && 'values' in payload && payload.values) {
		expect(payload.values).not.toHaveProperty('password');
	}
	expect(JSON.stringify(result)).not.toContain(submitted);
}

describe('admin page actions', () => {
	beforeEach(() => {
		engine.mockReset();
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
		const engineResult = await actions.resetPassword!({
			request: new Request('https://bookward.test/admin', { method: 'POST', body: engineBody })
		} as never);

		expect(engineResult).toMatchObject({
			status: 400,
			data: {
				formId: 'resetPassword-acct-42',
				error: true,
				fieldErrors: {}
			}
		});
		assertNoPasswordEcho(engineResult, fullCandidate);
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
	});
});
