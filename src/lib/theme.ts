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

export type ThemeSwatch = {
	primary: string;
	secondary: string;
	tertiary: string;
};

/**
 * Small preview tokens for the picker. These are intentionally separate from
 * the deferred theme styles: the picker can show every palette without
 * downloading all 24 full Skeleton stylesheets just to render three swatches.
 */
export const THEME_SWATCHES: Record<SkeletonThemeName, ThemeSwatch> = {
	catppuccin: { primary: 'oklch(66.37% 0.18 273.14deg)', secondary: 'oklch(72.56% 0.17 338.45deg)', tertiary: 'oklch(60.23% 0.1 201.09deg)' },
	cerberus: { primary: 'oklch(0.57 0.21 258.29)', secondary: 'oklch(0.49 0.23 300.45)', tertiary: 'oklch(0.65 0.26 2.47)' },
	concord: { primary: 'oklch(57.74% 0.21 273.85deg)', secondary: 'oklch(65.34% 0.22 351.93deg)', tertiary: 'oklch(69.62% 0.15 247.99deg)' },
	crimson: { primary: 'oklch(55.71% 0.21 19.55deg)', secondary: 'oklch(59.26% 0.09 239.95deg)', tertiary: 'oklch(78.4% 0.01 31.17deg)' },
	dracula: { primary: 'oklch(74.03% 0.15 302.13deg)', secondary: 'oklch(75% 0.18 346.86deg)', tertiary: 'oklch(88.11% 0.09 212.62deg)' },
	fennec: { primary: 'oklch(65.88% 0.21 38.25deg)', secondary: 'oklch(87.53% 0.1 74.15deg)', tertiary: 'oklch(57.22% 0.05 185.36deg)' },
	hamlindigo: { primary: 'oklch(80.28% 0.08 266.51deg)', secondary: 'oklch(65.46% 0.07 87.04deg)', tertiary: 'oklch(64.32% 0.06 213.24deg)' },
	legacy: { primary: 'oklch(69.84% 0.15 162.21deg)', secondary: 'oklch(51.06% 0.23 276.97deg)', tertiary: 'oklch(68.47% 0.15 237.31deg)' },
	mint: { primary: 'oklch(83.57% 0.18 148.98deg)', secondary: 'oklch(59.27% 0.21 282.75deg)', tertiary: 'oklch(44.74% 0.03 322.1deg)' },
	modern: { primary: 'oklch(65.59% 0.21 354.32deg)', secondary: 'oklch(71.48% 0.13 215.21deg)', tertiary: 'oklch(70.37% 0.12 182.49deg)' },
	mona: { primary: 'oklch(56.31% 0.21 294.98deg)', secondary: 'oklch(63.43% 0.16 148.39deg)', tertiary: 'oklch(81.11% 0.1 190.5deg)' },
	nosh: { primary: 'oklch(56.22% 0.23 24.62deg)', secondary: 'oklch(89.23% 0.04 17.93deg)', tertiary: 'oklch(42.89% 0.04 161.33deg)' },
	nouveau: { primary: 'oklch(83.44% 0.16 97deg)', secondary: 'oklch(56.7% 0.19 256.45deg)', tertiary: 'oklch(62.5% 0.15 284.38deg)' },
	pine: { primary: 'oklch(62.15% 0.08 79.85deg)', secondary: 'oklch(31.9% 0.11 347.8deg)', tertiary: 'oklch(61.68% 0.02 103.61deg)' },
	reign: { primary: 'oklch(94.82% 0.17 110.7deg)', secondary: 'oklch(94.82% 0.17 110.7deg)', tertiary: 'oklch(94.82% 0.17 110.7deg)' },
	rocket: { primary: 'oklch(71.48% 0.13 215.21deg)', secondary: 'oklch(62.31% 0.19 259.81deg)', tertiary: 'oklch(62.68% 0.23 303.91deg)' },
	rose: { primary: 'oklch(69.89% 0.13 348.12deg)', secondary: 'oklch(46.75% 0.22 272.16deg)', tertiary: 'oklch(78.41% 0.08 291.85deg)' },
	rosepine: { primary: 'oklch(53.06% 0.08 227.38deg)', secondary: 'oklch(78.02% 0.09 305.36deg)', tertiary: 'oklch(83.96% 0.05 21.29deg)' },
	sahara: { primary: 'oklch(78.19% 0.15 76.87deg)', secondary: 'oklch(76.32% 0.12 183.49deg)', tertiary: 'oklch(85.72% 0.12 126.76deg)' },
	seafoam: { primary: 'oklch(80.78% 0.07 190.34deg)', secondary: 'oklch(32.36% 0.07 262.2deg)', tertiary: 'oklch(65.36% 0.23 34.04deg)' },
	terminus: { primary: 'oklch(48.65% 0.3 279.02deg)', secondary: 'oklch(89.36% 0.16 171.7deg)', tertiary: 'oklch(91.3% 0.21 117.7deg)' },
	vintage: { primary: 'oklch(71.39% 0.16 59.66deg)', secondary: 'oklch(80.21% 0.08 152.14deg)', tertiary: 'oklch(71.48% 0.13 215.21deg)' },
	vox: { primary: 'oklch(82.71% 0.1 51.5deg)', secondary: 'oklch(92.54% 0.17 123.36deg)', tertiary: 'oklch(80.24% 0.12 298.53deg)' },
	wintry: { primary: 'oklch(62.31% 0.19 259.81deg)', secondary: 'oklch(68.47% 0.15 237.31deg)', tertiary: 'oklch(66.28% 0.18 280.87deg)' },
};

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
