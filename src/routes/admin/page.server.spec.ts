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
