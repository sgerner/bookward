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
export const DEFAULT_MODE = 'dark' as const;
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

export type ThemeColors = {
	light: string;
	dark: string;
};

const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

/**
 * Calibrated light/dark root background sRGB hex pairs (`--color-root-bg-light`
 * and `--color-root-bg-dark`) for `<meta name="theme-color">` and SSR bootstrap.
 */
export const THEME_COLORS: Record<SkeletonThemeName, ThemeColors> = {
	catppuccin: { light: '#ffffff', dark: '#131322' },
	cerberus: { light: '#fcfcfc', dark: '#121212' },
	concord: { light: '#ffffff', dark: '#17171c' },
	crimson: { light: '#ffffff', dark: '#0c0e17' },
	dracula: { light: '#f5f8ff', dark: '#12141e' },
	fennec: { light: '#eef6f1', dark: '#141519' },
	hamlindigo: { light: '#ffffff', dark: '#0d1427' },
	legacy: { light: '#ffffff', dark: '#0d1329' },
	mint: { light: '#e1f6ed', dark: '#001d17' },
	modern: { light: '#f2f2ff', dark: '#101229' },
	mona: { light: '#ffffff', dark: '#0c1522' },
	nosh: { light: '#f7f7f7', dark: '#030303' },
	nouveau: { light: '#fbf7fe', dark: '#070010' },
	pine: { light: '#f2f5ed', dark: '#12170d' },
	reign: { light: '#f0f0f0', dark: '#171717' },
	rocket: { light: '#ffffff', dark: '#0c1522' },
	rose: { light: '#f7f2f9', dark: '#161419' },
	rosepine: { light: '#f9f4ee', dark: '#181625' },
	sahara: { light: '#ffeff2', dark: '#260b0f' },
	seafoam: { light: '#e1f9fd', dark: '#001725' },
	terminus: { light: '#fcfcfc', dark: '#131313' },
	vintage: { light: '#f3f3f3', dark: '#181410' },
	vox: { light: '#f4f1ff', dark: '#190f26' },
	wintry: { light: '#ffffff', dark: '#091224' },
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
 * pre-calibrated at Vite build time (`vite.config.ts`) and loaded lazily only
 * when selected (or during SSR for the active palette), so the client neither
 * downloads unused palettes nor runs contrast calibration at runtime.
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

function extractStylesheet(theme: SkeletonThemeName, module: unknown): string | null {
	const stylesheet = typeof module === 'string'
		? module
		: (module as { default?: unknown } | null | undefined)?.default;
	if (typeof stylesheet !== 'string' || !stylesheet) return null;
	if (stylesheet.includes(`:root[data-theme="${theme}"], [data-theme="${theme}"]`)) {
		return stylesheet;
	}
	return stylesheet.replace(
		`[data-theme="${theme}"]`,
		`:root[data-theme="${theme}"], [data-theme="${theme}"]`,
	);
}

/** Resolve pre-calibrated CSS text for non-default themes during SSR and client loading. */
export async function loadThemeCss(theme: SkeletonThemeName): Promise<string | null> {
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	if (nextTheme === DEFAULT_THEME) return null;
	return extractStylesheet(nextTheme, await THEME_LOADERS[nextTheme]());
}

/** Load a palette stylesheet once before applying its data-theme attribute. */
export function loadTheme(theme: SkeletonThemeName): Promise<void> {
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	if (nextTheme === DEFAULT_THEME || typeof document === 'undefined') {
		return Promise.resolve();
	}
	if (
		typeof document.head?.querySelector === 'function' &&
		document.head.querySelector(`style[data-bookward-theme="${nextTheme}"]`)
	) {
		return Promise.resolve();
	}
	const existing = themeLoads.get(nextTheme);
	if (existing) return existing;

	const pending = loadThemeCss(nextTheme).then(
		(stylesheet) => {
			if (typeof document === 'undefined') return;
			if (document.head.querySelector(`style[data-bookward-theme="${nextTheme}"]`)) return;
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

export function getThemeColor(theme: SkeletonThemeName, resolvedMode: Exclude<ThemeMode, 'system'>): string {
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	const palette = THEME_COLORS[nextTheme];
	return resolvedMode === 'light' ? palette.light : palette.dark;
}

export function readCookie(cookieHeader: string | undefined, name: string): string | null {
	if (!cookieHeader) return null;
	for (const part of cookieHeader.split(';')) {
		const trimmed = part.trim();
		const eq = trimmed.indexOf('=');
		if (eq < 0) continue;
		if (trimmed.slice(0, eq) === name) {
			const value = trimmed.slice(eq + 1);
			try {
				return decodeURIComponent(value);
			} catch {
				return value;
			}
		}
	}
	return null;
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
		const rawCookies = typeof document !== 'undefined' && typeof document.cookie === 'string' ? document.cookie : undefined;
		const cookieTheme = storedTheme === null && legacyTheme === null ? readCookie(rawCookies, THEME_STORAGE_KEY) : null;
		const cookieMode = storedMode === null && legacyMode === null ? readCookie(rawCookies, MODE_STORAGE_KEY) : null;
		return {
			theme: isThemeName(storedTheme) ? storedTheme : isThemeName(legacyTheme) ? legacyTheme : isThemeName(cookieTheme) ? cookieTheme : DEFAULT_THEME,
			mode: isThemeMode(storedMode) ? storedMode : isThemeMode(legacyMode) ? legacyMode : isThemeMode(cookieMode) ? cookieMode : DEFAULT_MODE,
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

	const meta =
		typeof document.querySelector === 'function'
			? document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
			: typeof document.head?.querySelector === 'function'
				? document.head.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
				: null;
	if (meta) {
		const color = getThemeColor(nextTheme, resolved);
		if ('content' in meta) meta.content = color;
		if (typeof meta.setAttribute === 'function') meta.setAttribute('content', color);
	}
	return resolved;
}

export function persistPreferences(theme: SkeletonThemeName, mode: ThemeMode): void {
	if (typeof window === 'undefined') return;
	const nextTheme = isThemeName(theme) ? theme : DEFAULT_THEME;
	const nextMode = isThemeMode(mode) ? mode : DEFAULT_MODE;
	try {
		window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
		window.localStorage.setItem(MODE_STORAGE_KEY, nextMode);
	} catch {
		// Applying the current preference remains useful even when localStorage cannot persist.
	}
	try {
		if (typeof document !== 'undefined') {
			const secure = window.location?.protocol === 'https:' ? '; Secure' : '';
			const suffix = `; Path=/; Max-Age=${COOKIE_MAX_AGE}; SameSite=Lax${secure}`;
			document.cookie = `${THEME_STORAGE_KEY}=${nextTheme}${suffix}`;
			document.cookie = `${MODE_STORAGE_KEY}=${nextMode}${suffix}`;
		}
	} catch {
		// Sandboxed contexts can also block cookie writes.
	}
}

export function setTheme(theme: SkeletonThemeName, mode: ThemeMode): Exclude<ThemeMode, 'system'> {
	persistPreferences(theme, mode);
	return applyTheme(theme, mode);
}

/** Keep system mode and another tab's preference in sync while the app is open. */
export function initAppearance(): () => void {
	if (typeof window === 'undefined') return () => undefined;

	let generation = 0;
	let active = true;
	const sync = () => {
		const preferences = getStoredPreferences();
		const request = ++generation;
		void loadTheme(preferences.theme)
			.then(() => true, async () => {
				try {
					await loadTheme(DEFAULT_THEME);
				} catch {
					// Keep the currently loaded palette if even the bundled default fails.
				}
				return false;
			})
			.then((loaded) => {
				if (!active || request !== generation) return;
				applyTheme(loaded ? preferences.theme : DEFAULT_THEME, preferences.mode);
			});
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
		active = false;
		generation += 1;
		if (media && typeof media.removeEventListener === 'function') media.removeEventListener('change', handleSystemChange);
		else if (media && typeof media.removeListener === 'function') media.removeListener(handleSystemChange);
		window.removeEventListener('storage', sync);
	};
}
