import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn(),
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
	engine,
}));

import { EngineError } from '#lib/server/engine.js';
import { currentClientIp } from '#lib/server/request-context.js';
import { actions, load } from './+page.server';

const oidcConfig = { setup_required: false, local_login_enabled: true, oidc_enabled: true };

describe('login page loader', () => {
	beforeEach(() => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'false');
		engine.mockReset();	});
	afterEach(() => vi.unstubAllEnvs());

	it('starts configured SSO automatically and keeps the requested page', async () => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'true');
		engine.mockResolvedValue(oidcConfig);

		await expect(load({ url: new URL('https://bookward.test/login?next=%2Faccount') } as never))
			.rejects.toMatchObject({ status: 303, location: '/auth/oidc/start?intent=login&next=%2Faccount' });
	});

	it('does not auto-start SSO during setup, after an SSO failure, or on the explicit local-login route', async () => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'true');
		engine.mockResolvedValue(oidcConfig);
		const manual = await load({ url: new URL('https://bookward.test/login?manual=1') } as never);
		expect(manual).toMatchObject({ auto_sso: true, manual_login: true, oidc_enabled: true });

		const failed = await load({ url: new URL('https://bookward.test/login?error=unlinked&next=%2Faccount') } as never);
		expect(failed).toMatchObject({ next: '/account', error: expect.stringContaining('not linked yet') });

		engine.mockResolvedValue({ ...oidcConfig, setup_required: true });
		const setup = await load({ url: new URL('https://bookward.test/login') } as never);
		expect(setup).toMatchObject({ setup_required: true, auto_sso: true });
	});

	it('keeps the local form when no OIDC provider is configured', async () => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'true');
		engine.mockResolvedValue({ ...oidcConfig, oidc_enabled: false });

		const result = await load({ url: new URL('https://bookward.test/login') } as never);
		expect(result).toMatchObject({ oidc_enabled: false, auto_sso: false });
	});

	it('ignores an unsafe next URL before starting SSO', async () => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'true');
		engine.mockResolvedValue(oidcConfig);

		await expect(load({ url: new URL('https://bookward.test/login?next=%2F%2Fevil.example') } as never))
			.rejects.toMatchObject({ status: 303, location: '/auth/oidc/start?intent=login&next=%2F' });
	});

	it('forwards client IPs and distinguishes HTTP 429 wait guidance from HTTP 401 errors', async () => {
		let seenClientIp: string | undefined;
		engine.mockImplementationOnce(() => {
			seenClientIp = currentClientIp();
			return Promise.reject(new EngineError('Username or password is incorrect', 401));
		});
		const makeFormRequest = () => {
			const form = new FormData();
			form.set('username', 'bookward');
			form.set('password', 'wrong-password');
			return new Request('https://bookward.test/login?/login', { method: 'POST', body: form });
		};
		const setHeaders = vi.fn();

		const wrongPassword = await actions.login({
			request: makeFormRequest(),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login'),
			setHeaders,
			getClientAddress: () => '203.0.113.10'
		} as never);
		expect(seenClientIp).toBe('203.0.113.10');
		expect(wrongPassword).toMatchObject({
			status: 401,
			data: { message: 'Username or password is incorrect.' }
		});
		expect(setHeaders).not.toHaveBeenCalled();

		engine.mockRejectedValueOnce(new EngineError('Too many failed password attempts.', 429, 895));
		const lockedOut = await actions.login({
			request: makeFormRequest(),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login'),
			setHeaders,
			getClientAddress: () => '203.0.113.10'
		} as never);
		expect(setHeaders).toHaveBeenCalledWith({ 'retry-after': '895' });
		expect(lockedOut).toMatchObject({
			status: 429,
			data: {
				rateLimited: true,
				retryAfterSeconds: 895,
				message: expect.stringContaining('Wait 15 minutes')
			}
		});
	});

	it('omits Secure on HTTP setup and login session cookies and sets Secure on HTTPS', async () => {
		engine.mockResolvedValue({ session_token: 'admin-session', max_age: 86400 });
		const httpCookies = { set: vi.fn() };
		const body = new URLSearchParams({ username: 'admin', password: 'Strong-setup-password-1' });

		await expect(
			actions.setup({
				request: new Request('http://127.0.0.1:3000/login?/setup', {
					method: 'POST',
					headers: { 'content-type': 'application/x-www-form-urlencoded' },
					body
				}),
				cookies: httpCookies,
				url: new URL('http://127.0.0.1:3000/login')
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/' });

		expect(httpCookies.set).toHaveBeenCalledWith('bookward_session', 'admin-session', {
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/',
			maxAge: 86400
		});

		const httpsCookies = { set: vi.fn() };
		await expect(
			actions.login({
				request: new Request('https://books.example.com/login?/login', {
					method: 'POST',
					headers: { 'content-type': 'application/x-www-form-urlencoded' },
					body
				}),
				cookies: httpsCookies,
				url: new URL('https://books.example.com/login')
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/' });

		expect(httpsCookies.set).toHaveBeenCalledWith('bookward_session', 'admin-session', {
			httpOnly: true,
			secure: true,
			sameSite: 'lax',
			path: '/',
			maxAge: 86400
		});
	});
});
