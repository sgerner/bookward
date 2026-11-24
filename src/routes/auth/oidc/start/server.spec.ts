import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('#lib/server/engine.js', () => ({
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

	it('omits Secure on OIDC cookies over plain HTTP and matches delete flags on link intent', async () => {
		engine.mockResolvedValue({ authorization_url: 'https://auth.example/authorize', csrf: 'csrf-value' });
		const cookies = { set: vi.fn(), delete: vi.fn() };

		await expect(GET({
			url: new URL('http://192.168.1.50:3000/auth/oidc/start?intent=login&next=%2F'),
			cookies,
		} as never)).rejects.toMatchObject({ status: 302, location: 'https://auth.example/authorize' });

		for (const name of ['oidc_state', 'oidc_intent', 'oidc_next']) {
			expect(cookies.set).toHaveBeenCalledWith(name, expect.any(String), {
				httpOnly: true,
				secure: false,
				sameSite: 'lax',
				path: '/auth/oidc',
				maxAge: 600,
			});
		}

		const linkCookies = { set: vi.fn(), delete: vi.fn() };
		await expect(GET({
			url: new URL('http://192.168.1.50:3000/auth/oidc/start?intent=link'),
			cookies: linkCookies,
		} as never)).rejects.toMatchObject({ status: 302, location: 'https://auth.example/authorize' });
		expect(linkCookies.delete).toHaveBeenCalledWith('oidc_next', {
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/auth/oidc',
		});
	});

	it('returns to the login form when the provider returns an unsafe URL', async () => {
		engine.mockResolvedValue({ authorization_url: 'http://auth.example/authorize', csrf: 'csrf-value' });

		await expect(GET({
			url: new URL('https://bookward.test/auth/oidc/start?intent=login&next=%2Faccount'),
			cookies: { set: vi.fn(), delete: vi.fn() },
		} as never)).rejects.toMatchObject({ status: 303, location: '/login?error=sso&next=%2Faccount' });
	});
});
