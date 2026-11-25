import { randomUUID } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { engine } = vi.hoisted(() => ({
	engine: vi.fn(),
}));

vi.mock('#lib/server/engine.js', async (importOriginal) => {
	const actual = await importOriginal<typeof import('#lib/server/engine.js')>();
	return {
		...actual,
		engine,
	};
});

import { EngineError } from '#lib/server/engine.js';
import { actions, load } from './+page.server';

const oidcConfig = { setup_required: false, local_login_enabled: true, oidc_enabled: true };
const makeLongInput = () => randomUUID();
const makeShortInput = () => randomUUID().slice(0, 8);

function assertNoPasswordEcho(result: unknown, submitted: string) {
	const payload = (result as { data?: { values?: Record<string, unknown> } })?.data;
	expect(payload).not.toHaveProperty('password');
	expect(payload?.values).not.toHaveProperty('password');
	expect(JSON.stringify(result)).not.toContain(submitted);
}

describe('login page loader', () => {
	beforeEach(() => {
		vi.stubEnv('OIDC_AUTO_LOGIN', 'false');
		engine.mockReset();
	});
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

describe('login page actions', () => {
	beforeEach(() => {
		engine.mockReset();
	});

	it('returns fieldErrors, preserves username, and never echoes password when login validation fails', async () => {
		const candidate = makeLongInput();
		const body = new FormData();
		body.set('username', '   ');
		body.set('password', candidate);
		const result = await actions.login!({
			request: new Request('https://bookward.test/login', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(result).toMatchObject({
			status: 400,
			data: {
				formId: 'login',
				error: true,
				fieldErrors: { username: 'Enter your username.' },
				values: { username: '' }
			}
		});
		assertNoPasswordEcho(result, candidate);
		expect(engine).not.toHaveBeenCalled();
	});

	it('returns credentialError summary without per-field password error or password echo on 401 and 503 login failures', async () => {
		engine.mockRejectedValueOnce(new EngineError('Unauthorized', 401));
		const candidate = makeLongInput();
		const body = new FormData();
		body.set('username', 'reader');
		body.set('password', candidate);
		const result = await actions.login!({
			request: new Request('https://bookward.test/login', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(result).toMatchObject({
			status: 401,
			data: {
				formId: 'login',
				error: true,
				credentialError: true,
				message: 'Username or password is incorrect.',
				fieldErrors: {},
				values: { username: 'reader' }
			}
		});
		assertNoPasswordEcho(result, candidate);

		engine.mockRejectedValueOnce(new EngineError('Bookward engine is temporarily unavailable.', 503));
		const unavailableCandidate = makeLongInput();
		const unavailableBody = new FormData();
		unavailableBody.set('username', 'reader');
		unavailableBody.set('password', unavailableCandidate);
		const unavailableResult = await actions.login!({
			request: new Request('https://bookward.test/login', { method: 'POST', body: unavailableBody }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(unavailableResult).toMatchObject({
			status: 503,
			data: {
				formId: 'login',
				error: true,
				message: 'Bookward engine is temporarily unavailable.',
				fieldErrors: {},
				values: { username: 'reader' }
			}
		});
		assertNoPasswordEcho(unavailableResult, unavailableCandidate);
	});

	it('enforces the 12-character password rule during setup, preserves username, and never echoes password', async () => {
		const shortCandidate = makeShortInput();
		const body = new FormData();
		body.set('username', 'admin');
		body.set('password', shortCandidate);
		const validationResult = await actions.setup!({
			request: new Request('https://bookward.test/login', { method: 'POST', body }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(validationResult).toMatchObject({
			status: 400,
			data: {
				formId: 'setup',
				error: true,
				fieldErrors: { password: 'Use a password between 12 and 1024 characters.' },
				values: { username: 'admin' }
			}
		});
		assertNoPasswordEcho(validationResult, shortCandidate);
		expect(engine).not.toHaveBeenCalled();
	});

	it('maps setup engine errors, preserves username, and never echoes password across 400 and 403 responses', async () => {
		engine.mockRejectedValueOnce(new EngineError('Password cannot contain control characters.', 400));
		const rejectedCandidate = makeLongInput();
		const rejectedBody = new FormData();
		rejectedBody.set('username', 'admin');
		rejectedBody.set('password', rejectedCandidate);
		const rejectedResult = await actions.setup!({
			request: new Request('https://bookward.test/login', { method: 'POST', body: rejectedBody }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(rejectedResult).toMatchObject({
			status: 400,
			data: {
				formId: 'setup',
				error: true,
				fieldErrors: { password: 'Password cannot contain control characters.' },
				values: { username: 'admin' }
			}
		});
		assertNoPasswordEcho(rejectedResult, rejectedCandidate);

		engine.mockRejectedValueOnce(new EngineError('Initial setup has already completed.', 403));
		const lockedSetupCandidate = makeLongInput();
		const engineBody = new FormData();
		engineBody.set('username', 'admin');
		engineBody.set('password', lockedSetupCandidate);
		const engineResult = await actions.setup!({
			request: new Request('https://bookward.test/login', { method: 'POST', body: engineBody }),
			cookies: { set: vi.fn() },
			url: new URL('https://bookward.test/login')
		} as never);

		expect(engineResult).toMatchObject({
			status: 403,
			data: {
				formId: 'setup',
				error: true,
				fieldErrors: {},
				values: { username: 'admin' }
			}
		});
		assertNoPasswordEcho(engineResult, lockedSetupCandidate);
	});
});
