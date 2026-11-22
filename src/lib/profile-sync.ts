export const ACTIVE_PROFILE_STORAGE_KEY = 'bookward-active-profile';
export const MANUAL_LOGIN_PATH = '/login?manual=1';
export const LOGOUT_REDIRECT_DELAY_MS = 800;

export interface ProfileStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
	removeItem(key: string): void;
}

export interface ProfileChannelLike {
	postMessage(message: { type: 'logout' } | { type: 'profile'; profile_id: string }): void;
}

export function storedProfile(storage?: ProfileStorage | null): string | null {
	try {
		return storage?.getItem(ACTIVE_PROFILE_STORAGE_KEY) ?? null;
	} catch {
		return null;
	}
}

export function saveProfile(value: string, storage?: ProfileStorage | null): void {
	try {
		storage?.setItem(ACTIVE_PROFILE_STORAGE_KEY, value);
	} catch {
		/* Profile checks still run through the server session. */
	}
}

export function clearProfile(storage?: ProfileStorage | null): void {
	try {
		storage?.removeItem(ACTIVE_PROFILE_STORAGE_KEY);
	} catch {
		/* Profile checks still run through the server session. */
	}
}

export function isLogoutAction(action: string | null | undefined): boolean {
	return Boolean(action && action.includes('/auth/logout'));
}

export function handleLogoutSubmit(
	action: string | null | undefined,
	storage?: ProfileStorage | null,
	channel?: ProfileChannelLike | null
): boolean {
	if (!isLogoutAction(action)) return false;
	clearProfile(storage);
	channel?.postMessage({ type: 'logout' });
	return true;
}

export function handleProfileMessage(
	data: { type?: string; profile_id?: string } | null | undefined,
	options: {
		currentProfileId: string | null;
		storage?: ProfileStorage | null;
		replaceLocation: (url: string) => void;
		schedule?: (callback: () => void, delayMs: number) => unknown;
	}
): void {
	if (data?.type === 'logout') {
		clearProfile(options.storage);
		const schedule = options.schedule ?? ((fn, ms) => setTimeout(fn, ms));
		schedule(() => options.replaceLocation(MANUAL_LOGIN_PATH), LOGOUT_REDIRECT_DELAY_MS);
	} else if (data?.type === 'profile' && data.profile_id !== options.currentProfileId) {
		options.replaceLocation('/');
	}
}

export function syncActiveProfile(
	currentProfileId: string | null | undefined,
	options: {
		storage?: ProfileStorage | null;
		channel?: ProfileChannelLike | null;
		replaceLocation: (url: string) => void;
		checkStoredMismatch?: boolean;
	}
): void {
	if (!currentProfileId) {
		clearProfile(options.storage);
		return;
	}
	if (options.checkStoredMismatch) {
		const stored = storedProfile(options.storage);
		if (stored && stored !== currentProfileId) {
			options.replaceLocation('/');
		}
	}
	saveProfile(currentProfileId, options.storage);
	options.channel?.postMessage({ type: 'profile', profile_id: currentProfileId });
}
