import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({ engine: vi.fn() }));

vi.mock('#lib/server/engine.js', () => ({
	EngineError: class EngineError extends Error {
		status = 403;
	},
	engine,
}));

import { GET } from './+server';

describe('OIDC callback route', () => {
	beforeEach(() => engine.mockReset());

	it('returns a successful sign-in to the original safe route', async () => {
		engine.mockResolvedValue({ session_token: 'session', max_age: 3600 });
		const values: Record<string, string> = {
			oidc_state: 'csrf-value',
			oidc_intent: 'login',
			oidc_next: '/account?tab=security',
		};
		const cookies = {
			get: vi.fn((key: string) => values[key]),
			set: vi.fn(),
			delete: vi.fn(),
		};

		await expect(GET({
			url: new URL('https://bookward.test/auth/oidc/callback?code=code-value&state=state-value'),
			cookies,
		} as never)).rejects.toMatchObject({ status: 303, location: '/account?tab=security' });

		expect(cookies.delete).toHaveBeenCalledWith('oidc_next', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/auth/oidc',
		});
		expect(cookies.set).toHaveBeenCalledWith('bookward_session', 'session', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/',
			maxAge: 3600,
		});
	});

	it('omits Secure when deleting OIDC cookies and setting session cookies over HTTP', async () => {
		engine.mockResolvedValue({ session_token: 'lan-session', max_age: 3600 });
		const values: Record<string, string> = {
			oidc_state: 'csrf-value',
			oidc_intent: 'login',
			oidc_next: '/',
		};
		const cookies = { get: vi.fn((key: string) => values[key]), set: vi.fn(), delete: vi.fn() };

		await expect(GET({
			url: new URL('http://192.168.1.50:3000/auth/oidc/callback?code=code-value&state=state-value'),
			cookies,
		} as never)).rejects.toMatchObject({ status: 303, location: '/' });

		for (const name of ['oidc_state', 'oidc_intent', 'oidc_next']) {
			expect(cookies.delete).toHaveBeenCalledWith(name, {
				httpOnly: true,
				secure: false,
				sameSite: 'lax',
				path: '/auth/oidc',
			});
		}
		expect(cookies.set).toHaveBeenCalledWith('bookward_session', 'lan-session', {
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/',
			maxAge: 3600,
		});
	});

	it('rejects an unsafe saved route and falls back to the Bookward home page', async () => {
		engine.mockResolvedValue({ session_token: 'session', max_age: 3600 });
		const values: Record<string, string> = {
			oidc_state: 'csrf-value',
			oidc_intent: 'login',
			oidc_next: '//evil.example',
		};
		const cookies = { get: vi.fn((key: string) => values[key]), set: vi.fn(), delete: vi.fn() };

		await expect(GET({
			url: new URL('https://bookward.test/auth/oidc/callback?code=code-value&state=state-value'),
			cookies,
		} as never)).rejects.toMatchObject({ status: 303, location: '/' });
	});
});
