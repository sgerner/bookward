/**
 * The Skeleton theme catalog and the small amount of browser state needed to
 * apply it. Keeping this in one module means the no-flash bootstrap in
 * `app.html` and the hydrated picker share the same storage contract.
 */

export const THEME_STORAGE_KEY = 'bookward-theme';
export const MODE_STORAGE_KEY = 'bookward-color-mode';
const LEGACY_THEME_STORAGE_KEY = 'afterword-theme';
const LEGACY_MODE_STORAGE_KEY = 'afterword-color-mode';

export const DEFAULT_THEME = 'cerberus' as const;
export const DEFAULT_MODE = 'system' as const;
const SYSTEM_MEDIA_QUERY = '(prefers-color-scheme: dark)';

export type ThemeMode = 'light' | 'dark' | 'system';

export const THEMES = [
	{ id: 'catppuccin', label: 'Catppuccin' },
	{ id: 'cerberus', label: 'Cerberus' },
	{ id: 'concord', label: 'Concord' },
	{ id: 'crimson', label: 'Crimson' },
	{ id: 'dracula', label: 'Dracula' },
	{ id: 'fennec', label: 'Fennec' },
	{ id: 'hamlindigo', label: 'Hamlindigo' },
	{ id: 'legacy', label: 'Legacy' },
	{ id: 'mint', label: 'Mint' },
	{ id: 'modern', label: 'Modern' },
	{ id: 'mona', label: 'Mona' },
	{ id: 'nosh', label: 'Nosh' },
	{ id: 'nouveau', label: 'Nouveau' },
	{ id: 'pine', label: 'Pine' },
	{ id: 'reign', label: 'Reign' },
	{ id: 'rocket', label: 'Rocket' },
	{ id: 'rose', label: 'Rose' },
	{ id: 'rosepine', label: 'Rosepine' },
	{ id: 'sahara', label: 'Sahara' },
	{ id: 'seafoam', label: 'Seafoam' },
	{ id: 'terminus', label: 'Terminus' },
	{ id: 'vintage', label: 'Vintage' },
	{ id: 'vox', label: 'Vox' },
	{ id: 'wintry', label: 'Wintry' },
] as const;

export type SkeletonThemeName = (typeof THEMES)[number]['id'];

const THEME_NAMES = new Set<string>(THEMES.map((theme) => theme.id));

/**
 * Keep the default palette in the critical stylesheet. Alternate palettes are
 * loaded only when a user selects one, so the initial page does not download
 * every available theme.
 */
export const THEME_LOADERS: Record<SkeletonThemeName, () => Promise<unknown>> = {
	catppuccin: () => import('@skeletonlabs/skeleton/themes/catppuccin?raw'),
	cerberus: () => Promise.resolve(),
	concord: () => import('@skeletonlabs/skeleton/themes/concord?raw'),
	crimson: () => import('@skeletonlabs/skeleton/themes/crimson?raw'),
	dracula: () => import('@skeletonlabs/skeleton/themes/dracula?raw'),
	fennec: () => import('@skeletonlabs/skeleton/themes/fennec?raw'),
	hamlindigo: () => import('@skeletonlabs/skeleton/themes/hamlindigo?raw'),
	legacy: () => import('@skeletonlabs/skeleton/themes/legacy?raw'),
	mint: () => import('@skeletonlabs/skeleton/themes/mint?raw'),
	modern: () => import('@skeletonlabs/skeleton/themes/modern?raw'),
	mona: () => import('@skeletonlabs/skeleton/themes/mona?raw'),
	nosh: () => import('@skeletonlabs/skeleton/themes/nosh?raw'),
	nouveau: () => import('@skeletonlabs/skeleton/themes/nouveau?raw'),
	pine: () => import('@skeletonlabs/skeleton/themes/pine?raw'),
	reign: () => import('@skeletonlabs/skeleton/themes/reign?raw'),
	rocket: () => import('@skeletonlabs/skeleton/themes/rocket?raw'),
	rose: () => import('@skeletonlabs/skeleton/themes/rose?raw'),
	rosepine: () => import('@skeletonlabs/skeleton/themes/rosepine?raw'),
	sahara: () => import('@skeletonlabs/skeleton/themes/sahara?raw'),
	seafoam: () => import('@skeletonlabs/skeleton/themes/seafoam?raw'),
	terminus: () => import('@skeletonlabs/skeleton/themes/terminus?raw'),
	vintage: () => import('@skeletonlabs/skeleton/themes/vintage?raw'),
	vox: () => import('@skeletonlabs/skeleton/themes/vox?raw'),
	wintry: () => import('@skeletonlabs/skeleton/themes/wintry?raw'),
};

const themeLoads = new Map<SkeletonThemeName, Promise<void>>([
	[DEFAULT_THEME, Promise.resolve()],
]);

/** Load a palette stylesheet once before applying its data-theme attribute. */
export function loadTheme(theme: SkeletonThemeName): Promise<void> {
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	const existing = themeLoads.get(nextTheme);
	if (existing) return existing;

	const pending = THEME_LOADERS[nextTheme]().then(
		(module) => {
			if (nextTheme === DEFAULT_THEME || typeof document === 'undefined') return;
			if (document.head.querySelector(`style[data-bookward-theme="${nextTheme}"]`)) return;
			const stylesheet = typeof module === 'string'
				? module
				: (module as { default?: unknown }).default;
			if (typeof stylesheet !== 'string') throw new Error(`Theme stylesheet for ${nextTheme} was unavailable`);
			const style = document.createElement('style');
			style.dataset.bookwardTheme = nextTheme;
			style.textContent = stylesheet;
			document.head.append(style);
		},
		(error) => {
			themeLoads.delete(nextTheme);
			throw error;
		},
	);
	themeLoads.set(nextTheme, pending);
	return pending;
}

export function isThemeName(value: unknown): value is SkeletonThemeName {
	return typeof value === 'string' && THEME_NAMES.has(value);
}

export function isThemeMode(value: unknown): value is ThemeMode {
	return value === 'light' || value === 'dark' || value === 'system';
}

export function getSystemMode(): Exclude<ThemeMode, 'system'> {
	try {
		return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(SYSTEM_MEDIA_QUERY).matches
			? 'dark'
			: 'light';
	} catch {
		// A restricted webview may expose window without exposing matchMedia.
		return 'light';
	}
}

export function resolveMode(mode: ThemeMode): Exclude<ThemeMode, 'system'> {
	return mode === 'system' ? getSystemMode() : mode;
}

export function getStoredPreferences(): { theme: SkeletonThemeName; mode: ThemeMode } {
	try {
		if (typeof window === 'undefined') return { theme: DEFAULT_THEME, mode: DEFAULT_MODE };
		const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
		const legacyTheme = window.localStorage.getItem(LEGACY_THEME_STORAGE_KEY);
		const storedMode = window.localStorage.getItem(MODE_STORAGE_KEY);
		const legacyMode = window.localStorage.getItem(LEGACY_MODE_STORAGE_KEY);
		return {
			theme: isThemeName(storedTheme) ? storedTheme : isThemeName(legacyTheme) ? legacyTheme : DEFAULT_THEME,
			mode: isThemeMode(storedMode) ? storedMode : isThemeMode(legacyMode) ? legacyMode : DEFAULT_MODE,
		};
	} catch {
		// Private browsing and embedded webviews can deny localStorage access.
		const root = typeof document === 'undefined' ? undefined : document.documentElement;
		return {
			theme: isThemeName(root?.dataset.theme) ? root.dataset.theme : DEFAULT_THEME,
			mode: isThemeMode(root?.dataset.mode) ? root.dataset.mode : DEFAULT_MODE,
		};
	}
}

/** Apply Skeleton's dual light/dark tokens to the document root. */
export function applyTheme(theme: SkeletonThemeName, mode: ThemeMode): Exclude<ThemeMode, 'system'> {
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	const nextMode = isThemeMode(mode) ? mode : DEFAULT_MODE;
	if (typeof document === 'undefined') return resolveMode(nextMode);

	const resolved = resolveMode(nextMode);
	const root = document.documentElement;
	root.dataset.theme = nextTheme;
	root.dataset.mode = nextMode;
	root.dataset.colorScheme = resolved;
	root.classList.toggle('dark', resolved === 'dark');
	root.style.colorScheme = resolved;
	return resolved;
}

export function persistPreferences(theme: SkeletonThemeName, mode: ThemeMode): void {
	try {
		if (typeof window === 'undefined') return;
		window.localStorage.setItem(THEME_STORAGE_KEY, isThemeName(theme) ? theme : DEFAULT_THEME);
		window.localStorage.setItem(MODE_STORAGE_KEY, isThemeMode(mode) ? mode : DEFAULT_MODE);
	} catch {
		// Applying the current preference remains useful even when it cannot persist.
	}
}

export function setTheme(theme: SkeletonThemeName, mode: ThemeMode): Exclude<ThemeMode, 'system'> {
	persistPreferences(theme, mode);
	return applyTheme(theme, mode);
}

/** Keep system mode and another tab's preference in sync while the app is open. */
export function initAppearance(): () => void {
	if (typeof window === 'undefined') return () => undefined;

	const sync = () => {
		const preferences = getStoredPreferences();
		applyTheme(preferences.theme, preferences.mode);
	};
	const media = typeof window.matchMedia === 'function' ? window.matchMedia(SYSTEM_MEDIA_QUERY) : undefined;
	const handleSystemChange = () => {
		if (getStoredPreferences().mode === 'system') sync();
	};

	sync();
	if (media && typeof media.addEventListener === 'function') media.addEventListener('change', handleSystemChange);
	else if (media && typeof media.addListener === 'function') media.addListener(handleSystemChange);
	window.addEventListener('storage', sync);
	return () => {
		if (media && typeof media.removeEventListener === 'function') media.removeEventListener('change', handleSystemChange);
		else if (media && typeof media.removeListener === 'function') media.removeListener(handleSystemChange);
		window.removeEventListener('storage', sync);
	};
}
