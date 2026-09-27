import { beforeEach, describe, expect, it, vi } from 'vitest';

const { env, engine } = vi.hoisted(() => ({
	env: { OIDC_AUTO_LOGIN: 'false' as string },
	engine: vi.fn(),
}));

vi.mock('$env/dynamic/private', () => ({ env }));
vi.mock('$lib/server/engine', () => ({
	EngineError: class EngineError extends Error {
		status = 500;
	},
	engine,
}));

import { load } from './+page.server';

const oidcConfig = { setup_required: false, local_login_enabled: true, oidc_enabled: true };

describe('login page loader', () => {
	beforeEach(() => {
		env.OIDC_AUTO_LOGIN = 'false';
		engine.mockReset();
	});

	it('starts configured SSO automatically and keeps the requested page', async () => {
		env.OIDC_AUTO_LOGIN = 'true';
		engine.mockResolvedValue(oidcConfig);

		await expect(load({ url: new URL('https://bookward.test/login?next=%2Faccount') } as never))
			.rejects.toMatchObject({ status: 303, location: '/auth/oidc/start?intent=login&next=%2Faccount' });
	});

	it('does not auto-start SSO during setup, after an SSO failure, or on the explicit local-login route', async () => {
		env.OIDC_AUTO_LOGIN = 'true';
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
		env.OIDC_AUTO_LOGIN = 'true';
		engine.mockResolvedValue({ ...oidcConfig, oidc_enabled: false });

		const result = await load({ url: new URL('https://bookward.test/login') } as never);
		expect(result).toMatchObject({ oidc_enabled: false, auto_sso: false });
	});

	it('ignores an unsafe next URL before starting SSO', async () => {
		env.OIDC_AUTO_LOGIN = 'true';
		engine.mockResolvedValue(oidcConfig);

		await expect(load({ url: new URL('https://bookward.test/login?next=%2F%2Fevil.example') } as never))
			.rejects.toMatchObject({ status: 303, location: '/auth/oidc/start?intent=login&next=%2F' });
	});
});
