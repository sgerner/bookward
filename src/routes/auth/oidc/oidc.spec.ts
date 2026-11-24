import { beforeEach, describe, expect, it, vi } from 'vitest';

const { engine, MockEngineError } = vi.hoisted(() => {
	class HoistedEngineError extends Error {
		constructor(
			message: string,
			readonly status: number
		) {
			super(message);
		}
	}
	return {
		engine: vi.fn(),
		MockEngineError: HoistedEngineError
	};
});

vi.mock('#lib/server/engine.js', () => ({
	EngineError: MockEngineError,
	engine
}));

import { GET as startGet } from './start/+server';
import { GET as callbackGet } from './callback/+server';

describe('OIDC enrollment verification routes', () => {
	beforeEach(() => engine.mockReset());

	it('starts intent=enroll transactions and routes unauthenticated or unlinked errors cleanly', async () => {
		const cookies = {
			set: vi.fn(),
			delete: vi.fn()
		};
		engine.mockResolvedValueOnce({
			authorization_url: 'https://idp.example/authorize?state=s1&prompt=login',
			csrf: 'csrf-token'
		});

		await expect(
			startGet({
				url: new URL('https://bookward.test/auth/oidc/start?intent=enroll&profile_id=sso-profile'),
				cookies
			} as never)
		).rejects.toMatchObject({
			status: 302,
			location: 'https://idp.example/authorize?state=s1&prompt=login'
		});

		expect(engine).toHaveBeenCalledWith('/auth/oidc/start?intent=enroll');
		expect(cookies.set).toHaveBeenCalledWith(
			'oidc_intent',
			'enroll',
			expect.objectContaining({ httpOnly: true, path: '/auth/oidc' })
		);
		expect(cookies.delete).toHaveBeenCalledWith('oidc_next', { path: '/auth/oidc' });

		engine.mockRejectedValueOnce(new MockEngineError('Authentication required', 401));
		await expect(
			startGet({
				url: new URL('https://bookward.test/auth/oidc/start?intent=enroll'),
				cookies
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/login?next=%2Faccount' });

		engine.mockRejectedValueOnce(new MockEngineError('No linked SSO identity', 400));
		await expect(
			startGet({
				url: new URL('https://bookward.test/auth/oidc/start?intent=enroll'),
				cookies
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/account?enroll_error=1' });
	});

	it('redirects verified enrollment callbacks to /account?enroll=1 and errors to /account?enroll_error=1', async () => {
		const cookieJar = (values: Record<string, string>) => ({
			get: (name: string) => values[name],
			set: vi.fn(),
			delete: vi.fn()
		});

		engine.mockResolvedValueOnce({ enroll_verified: true });
		await expect(
			callbackGet({
				url: new URL('https://bookward.test/auth/oidc/callback?code=c1&state=s1'),
				cookies: cookieJar({ oidc_state: 'csrf-token', oidc_intent: 'enroll' })
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/account?enroll=1' });

		engine.mockRejectedValueOnce(new MockEngineError('Wrong subject', 403));
		await expect(
			callbackGet({
				url: new URL('https://bookward.test/auth/oidc/callback?code=c2&state=s2'),
				cookies: cookieJar({ oidc_state: 'csrf-token', oidc_intent: 'enroll' })
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/account?enroll_error=1' });

		await expect(
			callbackGet({
				url: new URL('https://bookward.test/auth/oidc/callback?error=access_denied'),
				cookies: cookieJar({ oidc_state: 'csrf-token', oidc_intent: 'enroll' })
			} as never)
		).rejects.toMatchObject({ status: 303, location: '/account?enroll_error=1' });
	});
});
