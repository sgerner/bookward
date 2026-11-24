import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('#lib/server/engine.js', () => ({ engine }));

import { POST } from './+server';

describe('logout route', () => {
	beforeEach(() => engine.mockReset());

	it('keeps the browser on the manual sign-in form when automatic OIDC is enabled', async () => {
		engine.mockResolvedValue(undefined);
		const httpCookies = { delete: vi.fn() };

		await expect(
			POST({ cookies: httpCookies, url: new URL('http://127.0.0.1:3000/auth/logout') } as never)
		).rejects.toMatchObject({
			status: 303,
			location: '/login?manual=1',
		});
		expect(httpCookies.delete).toHaveBeenCalledWith('bookward_session', {
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/',
		});

		const httpsCookies = { delete: vi.fn() };
		await expect(
			POST({ cookies: httpsCookies, url: new URL('https://books.example.com/auth/logout') } as never)
		).rejects.toMatchObject({
			status: 303,
			location: '/login?manual=1',
		});
		expect(httpsCookies.delete).toHaveBeenCalledWith('bookward_session', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/',
		});
	});
});
