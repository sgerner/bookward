import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('$lib/server/engine', () => ({ engine }));

import { POST } from './+server';

describe('logout route', () => {
	beforeEach(() => engine.mockReset());

	it('keeps the browser on the manual sign-in form when automatic OIDC is enabled', async () => {
		engine.mockResolvedValue(undefined);
		const cookies = { delete: vi.fn() };

		await expect(POST({ cookies } as never)).rejects.toMatchObject({
			status: 303,
			location: '/login?manual=1',
		});
		expect(cookies.delete).toHaveBeenCalledWith('bookward_session', { path: '/' });
	});
});
