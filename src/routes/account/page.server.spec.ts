import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		constructor(message: string, readonly status: number) {
			super(message);
		}
	},
	engine
}));

import { load } from './+page.server';

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
