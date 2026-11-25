import { randomUUID } from 'node:crypto';
import { render } from 'svelte/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PageData } from './$types';

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
import AdminPage from './+page.svelte';

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

describe('admin identity backup recovery hold', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('loads identity recovery hold status and requires confirmation before dismissal', async () => {
		const accounts = {
			accounts: [
				{
					id: 'admin-1',
					profile_id: 'legacy',
					username: 'owner',
					display_name: 'Owner',
					role: 'admin',
					status: 'active'
				}
			]
		};
		const identityBackup = {
			enabled: true,
			interval_hours: 24,
			retention_count: 7,
			recovery_hold: true,
			recovery_hold_at: '2026-11-21T20:00:00+00:00',
			last_success_at: '2026-11-20T19:00:00+00:00',
			last_attempt_at: null,
			last_error: null,
			last_restore_at: null,
			backups: [
				{
					id: 'bookward-auth-20261120T190000Z-aaaa1111',
					created_at: '2026-11-20T19:00:00+00:00',
					size_bytes: 32768
				},
				{
					id: 'bookward-auth-20261119T190000Z-bbbb2222',
					created_at: '2026-11-19T19:00:00+00:00',
					size_bytes: 30720
				}
			]
		};

		engine.mockImplementation((path: string) =>
			Promise.resolve(path.includes('/admin/auth-backups') ? identityBackup : accounts)
		);

		const data = (await load({
			locals: {
				user: {
					id: 'admin-1',
					profile_id: 'legacy',
					username: 'owner',
					display_name: 'Owner',
					role: 'admin',
					must_change_password: false
				}
			}
		} as never)) as Omit<PageData, 'user'>;

		expect(data.identityBackup?.recovery_hold).toBe(true);
		expect(data.identityBackup?.backups[0].id).toBe(
			'bookward-auth-20261120T190000Z-aaaa1111'
		);

		const rendered = render(AdminPage, {
			props: {
				data: {
					...data,
					user: {
						id: 'admin-1',
						profile_id: 'legacy',
						username: 'owner',
						display_name: 'Owner',
						role: 'admin',
						must_change_password: false
					}
				},
				form: null
			}
		});
		expect(rendered.body).toContain('Recovery hold · backups paused');
		expect(rendered.body).toContain(
			'afterword_engine.auth_admin restore bookward-auth-20261120T190000Z-aaaa1111 --confirm'
		);
		expect(rendered.body).toContain(
			'value="bookward-auth-20261120T190000Z-aaaa1111" selected'
		);
		expect(rendered.body).toContain('?/dismissIdentityHold');

		engine.mockReset();
		const rejected = await actions.dismissIdentityHold!({
			request: new Request('https://bookward.test/admin', {
				method: 'POST',
				body: new FormData()
			})
		} as never);
		expect(rejected).toMatchObject({
			status: 400,
			data: {
				formId: 'dismissIdentityHold',
				error: true
			}
		});
		expect(engine).not.toHaveBeenCalled();

		engine.mockResolvedValueOnce({ dismissed: true });
		const body = new FormData();
		body.set('confirm', 'KEEP');
		await expect(
			actions.dismissIdentityHold!({
				request: new Request('https://bookward.test/admin', {
					method: 'POST',
					body
				})
			} as never)
		).resolves.toEqual({
			formId: 'dismissIdentityHold',
			message:
				'Account-registry recovery hold dismissed. Scheduled backups have resumed.'
		});
		expect(engine).toHaveBeenCalledWith('/admin/auth-backups/recovery-hold/dismiss', {
			method: 'POST',
			body: JSON.stringify({ confirm: true })
		});
	});
});

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
	});
});
