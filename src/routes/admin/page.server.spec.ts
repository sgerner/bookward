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
import { actions } from './+page.server';

describe('admin page actions', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('maps a 404 unlock target through failure() with error=true and exact status', async () => {
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
				message: 'Account not found.',
				error: true
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
				message: 'Choose a valid account.',
				error: true
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
				message: 'fetch failed',
				error: true
			}
		});
	});

	it('formats unlock confirmation messages when attempts were cleared or already zero', async () => {
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
			message: 'Sign-in lockouts cleared for reader.'
		});
	});
});
