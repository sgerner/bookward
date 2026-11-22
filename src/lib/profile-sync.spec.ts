import { describe, expect, it, vi } from 'vitest';
import {
	ACTIVE_PROFILE_STORAGE_KEY,
	LOGOUT_REDIRECT_DELAY_MS,
	MANUAL_LOGIN_PATH,
	handleLogoutSubmit,
	handleProfileMessage,
	storedProfile,
	syncActiveProfile,
	type ProfileStorage,
} from './profile-sync.js';

function memoryStorage(initial?: Record<string, string>): ProfileStorage {
	const store = new Map<string, string>(Object.entries(initial ?? {}));
	return {
		getItem: (key) => store.get(key) ?? null,
		setItem: (key, value) => {
			store.set(key, value);
		},
		removeItem: (key) => {
			store.delete(key);
		},
	};
}

describe('cross-tab logout and profile storage sync', () => {
	it('clears bookward-active-profile on logout submit and broadcasts logout', () => {
		const storage = memoryStorage({ [ACTIVE_PROFILE_STORAGE_KEY]: 'alice-profile' });
		const postMessage = vi.fn();

		const handled = handleLogoutSubmit('https://bookward.test/auth/logout', storage, { postMessage });

		expect(handled).toBe(true);
		expect(storedProfile(storage)).toBeNull();
		expect(postMessage).toHaveBeenCalledWith({ type: 'logout' });
	});

	it('sends sibling tabs to /login?manual=1 and clears stored profile on logout broadcast', () => {
		const storage = memoryStorage({ [ACTIVE_PROFILE_STORAGE_KEY]: 'alice-profile' });
		const replaceLocation = vi.fn();
		const scheduled: Array<{ callback: () => void; delayMs: number }> = [];

		handleProfileMessage(
			{ type: 'logout' },
			{
				currentProfileId: 'alice-profile',
				storage,
				replaceLocation,
				schedule: (callback, delayMs) => scheduled.push({ callback, delayMs }),
			}
		);

		expect(storedProfile(storage)).toBeNull();
		expect(scheduled).toHaveLength(1);
		expect(scheduled[0].delayMs).toBe(LOGOUT_REDIRECT_DELAY_MS);
		scheduled[0].callback();
		expect(replaceLocation).toHaveBeenCalledWith(MANUAL_LOGIN_PATH);
	});

	it('preserves post-login deep links after logout when signing in as another profile', () => {
		const storage = memoryStorage({ [ACTIVE_PROFILE_STORAGE_KEY]: 'alice-profile' });
		const replaceLocation = vi.fn();
		const postMessage = vi.fn();

		// Unauthenticated /login?manual=1 or logout clears the cached profile.
		syncActiveProfile(null, {
			storage,
			channel: { postMessage },
			replaceLocation,
			checkStoredMismatch: true,
		});
		expect(storedProfile(storage)).toBeNull();

		// Subsequent login to /?digest=1 as Bob keeps the deep link intact.
		syncActiveProfile('bob-profile', {
			storage,
			channel: { postMessage },
			replaceLocation,
			checkStoredMismatch: true,
		});
		expect(replaceLocation).not.toHaveBeenCalled();
		expect(storedProfile(storage)).toBe('bob-profile');
		expect(postMessage).toHaveBeenCalledWith({ type: 'profile', profile_id: 'bob-profile' });
	});

	it('redirects stale active tabs to / when an active profile changes without logout', () => {
		const storage = memoryStorage({ [ACTIVE_PROFILE_STORAGE_KEY]: 'alice-profile' });
		const replaceLocation = vi.fn();

		syncActiveProfile('bob-profile', {
			storage,
			replaceLocation,
			checkStoredMismatch: true,
		});

		expect(replaceLocation).toHaveBeenCalledWith('/');
		expect(storedProfile(storage)).toBe('bob-profile');
	});
});
