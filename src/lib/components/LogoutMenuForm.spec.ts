import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import LogoutMenuForm from './LogoutMenuForm.svelte';
import { createLogoutSubmit } from '#lib/reader-form-actions.js';

describe('LogoutMenuForm and createLogoutSubmit', () => {
	it('renders an enhanced POST /auth/logout form with ProfileField and busy state', () => {
		const idle = render(LogoutMenuForm, {
			props: {
				profileId: 'alice-profile',
				pending: false,
				onSubmit: () => {}
			}
		});
		expect(idle.body).toContain('method="POST"');
		expect(idle.body).toContain('action="/auth/logout"');
		expect(idle.body).toContain('name="__profile_id" value="alice-profile"');
		expect(idle.body).toContain('Sign out');
		expect(idle.body).toContain('aria-busy="false"');

		const busy = render(LogoutMenuForm, {
			props: {
				profileId: 'alice-profile',
				pending: true,
				onSubmit: () => {}
			}
		});
		expect(busy.body).toContain('disabled');
		expect(busy.body).toContain('aria-busy="true"');
	});

	it('notifies sibling tabs and calls update() without error when /auth/logout redirects 303', async () => {
		const pending: boolean[] = [];
		const closeMenu = vi.fn();
		const announceError = vi.fn();
		const notifyLogout = vi.fn();
		const update = vi.fn().mockResolvedValue(undefined);

		const submit = createLogoutSubmit({
			setPending: (value) => pending.push(value),
			closeMenu,
			announceError,
			notifyLogout
		});

		const callback = submit({} as never);
		expect(pending).toEqual([true]);

		await (callback as (arg: unknown) => Promise<void>)({
			update,
			result: { type: 'redirect', status: 303, location: '/login?manual=1' }
		});

		expect(closeMenu).toHaveBeenCalledTimes(1);
		expect(announceError).not.toHaveBeenCalled();
		expect(notifyLogout).toHaveBeenCalledTimes(1);
		expect(update).toHaveBeenCalledTimes(1);
		expect(pending).toEqual([true, false]);
	});

	it('announces error and skips logout broadcast when /auth/logout returns 409 or error', async () => {
		const closeMenu = vi.fn();
		const announceError = vi.fn();
		const notifyLogout = vi.fn();
		const update = vi.fn();

		const submit = createLogoutSubmit({
			setPending: () => {},
			closeMenu,
			announceError,
			notifyLogout
		});

		const callback = submit({} as never);
		await (callback as (arg: unknown) => Promise<void>)({
			update,
			result: {
				type: 'error',
				status: 409,
				error: {
					message: 'This page belongs to a different profile. Reload before continuing.'
				}
			}
		});

		expect(closeMenu).toHaveBeenCalledTimes(1);
		expect(announceError).toHaveBeenCalledWith(
			'This page belongs to a different profile. Reload before continuing.'
		);
		expect(notifyLogout).not.toHaveBeenCalled();
		expect(update).not.toHaveBeenCalled();
	});
});
