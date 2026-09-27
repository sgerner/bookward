import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('$lib/server/engine', () => ({
	EngineError: class EngineError extends Error {
		status = 500;
	},
	engine,
}));

import { GET } from './+server';

describe('OIDC login start route', () => {
	beforeEach(() => engine.mockReset());

	it('stores a safe return target in a short-lived callback-only cookie', async () => {
		engine.mockResolvedValue({ authorization_url: 'https://auth.example/authorize', csrf: 'csrf-value' });
		const cookies = { set: vi.fn(), delete: vi.fn() };

		await expect(GET({
			url: new URL('https://bookward.test/auth/oidc/start?intent=login&next=%2Faccount%3Ftab%3Dsecurity'),
			cookies,
		} as never)).rejects.toMatchObject({ status: 302, location: 'https://auth.example/authorize' });

		expect(cookies.set).toHaveBeenCalledWith('oidc_next', '/account?tab=security', expect.objectContaining({
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/auth/oidc',
			maxAge: 600,
		}));
	});

	it('returns to the login form when the provider returns an unsafe URL', async () => {
		engine.mockResolvedValue({ authorization_url: 'http://auth.example/authorize', csrf: 'csrf-value' });

		await expect(GET({
			url: new URL('https://bookward.test/auth/oidc/start?intent=login&next=%2Faccount'),
			cookies: { set: vi.fn(), delete: vi.fn() },
		} as never)).rejects.toMatchObject({ status: 303, location: '/login?error=sso&next=%2Faccount' });
	});
});
