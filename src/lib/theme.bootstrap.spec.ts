import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';
import { THEME_COLORS, type SkeletonThemeName } from './theme';

type BootstrapRoot = {
	dataset: Record<string, string>;
	style: { colorScheme: string };
	classes: Set<string>;
	classList: { toggle: (name: string, force?: boolean) => boolean };
	metaThemeColor: string;
	cookies: Map<string, string>;
	cookieWrites: string[];
};

const appHtml = readFileSync(join(process.cwd(), 'src/app.html'), 'utf8');
const headMarker = appHtml.indexOf('%sveltekit.head%');
const scriptStart = appHtml.indexOf('<script>');
const scriptEnd = scriptStart < 0 ? -1 : appHtml.indexOf('</script>', scriptStart + '<script>'.length);
const bootstrap = scriptStart >= 0 && scriptEnd >= 0
	? appHtml.slice(scriptStart + '<script>'.length, scriptEnd).trim()
	: undefined;

function executeBootstrap(options: {
	readStorage?: (key: string) => string | null;
	systemDark?: boolean;
	denyStorage?: boolean;
	withoutMatchMedia?: boolean;
	inlineThemes?: SkeletonThemeName[];
	initialCookies?: Record<string, string>;
	protocol?: 'http:' | 'https:';
}): BootstrapRoot {
	if (!bootstrap) throw new Error('app.html is missing the appearance bootstrap');

	const classes = new Set<string>();
	const cookies = new Map<string, string>(Object.entries(options.initialCookies ?? {}));
	const cookieWrites: string[] = [];
	const inlineThemeSet = new Set<string>(options.inlineThemes ?? []);
	const meta = {
		content: THEME_COLORS.cerberus.dark,
		setAttribute(name: string, value: string) {
			if (name === 'content') meta.content = value;
		},
	};
	const root: BootstrapRoot = {
		dataset: { theme: 'cerberus', mode: 'dark', colorScheme: 'dark' },
		style: { colorScheme: '' },
		classes,
		get metaThemeColor() {
			return meta.content;
		},
		cookies,
		cookieWrites,
		classList: {
			toggle: (name, force) => {
				const enabled = force ?? !classes.has(name);
				if (enabled) classes.add(name);
				else classes.delete(name);
				return enabled;
			},
		},
	};

	const querySelector = (selector: string) => {
		if (selector === 'meta[name="theme-color"]') return meta;
		const styleMatch = selector.match(/^style\[data-bookward-theme="([^"]+)"\]$/);
		if (styleMatch && inlineThemeSet.has(styleMatch[1])) return {};
		return null;
	};

	const documentObject: Record<string, unknown> = {
		documentElement: root,
		head: { querySelector },
		querySelector,
	};
	Object.defineProperty(documentObject, 'cookie', {
		configurable: true,
		get: () => [...cookies.entries()].map(([key, value]) => `${key}=${value}`).join('; '),
		set: (raw: string) => {
			cookieWrites.push(raw);
			const [pair] = raw.split(';');
			const eq = pair.indexOf('=');
			if (eq >= 0) cookies.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
		},
	});

	const windowObject: {
		localStorage?: unknown;
		matchMedia?: () => { matches: boolean };
		location?: { protocol: string };
	} = {};
	if (options.protocol) windowObject.location = { protocol: options.protocol };
	if (!options.withoutMatchMedia) windowObject.matchMedia = () => ({ matches: options.systemDark ?? false });
	if (options.denyStorage) {
		Object.defineProperty(windowObject, 'localStorage', {
			configurable: true,
			get: () => {
				throw new Error('storage denied');
			},
		});
	} else {
		windowObject.localStorage = {
			getItem: options.readStorage ?? (() => null),
		};
	}

	runInNewContext(bootstrap, {
		window: windowObject,
		document: documentObject,
	});
	return root;
}

describe('no-flash appearance bootstrap', () => {
	it('runs after %sveltekit.head% so SSR style and meta tags exist', () => {
		expect(headMarker).toBeGreaterThan(-1);
		expect(scriptStart).toBeGreaterThan(headMarker);
	});

	it.each([
		{ mode: 'dark', systemDark: false, resolved: 'dark', hasDarkClass: true },
		{ mode: 'light', systemDark: true, resolved: 'light', hasDarkClass: false },
		{ mode: 'system', systemDark: true, resolved: 'dark', hasDarkClass: true },
		{ mode: 'system', systemDark: false, resolved: 'light', hasDarkClass: false },
	] as const)(
		'applies saved $mode preferences (systemDark=$systemDark) when SSR theme CSS is present',
		({ mode, systemDark, resolved, hasDarkClass }) => {
			const root = executeBootstrap({
				systemDark,
				inlineThemes: ['rocket'],
				readStorage: (key) => (key === 'afterword-theme' ? 'rocket' : mode),
			});

			expect(root.dataset.theme).toBe('rocket');
			expect(root.dataset.mode).toBe(mode);
			expect(root.dataset.colorScheme).toBe(resolved);
			expect(root.style.colorScheme).toBe(resolved);
			expect(root.classes.has('dark')).toBe(hasDarkClass);
			expect(root.metaThemeColor).toBe(THEME_COLORS.rocket[resolved]);
			expect(root.cookies.get('bookward-theme')).toBe('rocket');
			expect(root.cookies.get('bookward-color-mode')).toBe(mode);
			expect(root.cookieWrites).toEqual(
				expect.arrayContaining([
					expect.stringContaining('bookward-theme=rocket; Path=/; Max-Age=31536000; SameSite=Lax'),
					expect.stringContaining(`bookward-color-mode=${mode}; Path=/; Max-Age=31536000; SameSite=Lax`),
				]),
			);
		},
	);

	it('syncs localStorage to cookies and falls back to cerberus before hydration when non-default SSR CSS is absent', () => {
		const root = executeBootstrap({
			systemDark: false,
			protocol: 'https:',
			readStorage: (key) => {
				if (key === 'bookward-theme') return 'sahara';
				if (key === 'bookward-color-mode') return 'light';
				return null;
			},
		});

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('light');
		expect(root.dataset.colorScheme).toBe('light');
		expect(root.classes.has('dark')).toBe(false);
		expect(root.metaThemeColor).toBe(THEME_COLORS.cerberus.light);
		expect(root.cookies.get('bookward-theme')).toBe('sahara');
		expect(root.cookies.get('bookward-color-mode')).toBe('light');
		expect(root.cookieWrites.every((entry) => entry.includes('; SameSite=Lax; Secure'))).toBe(true);
	});

	it('applies cookie-backed SSR theme and resolves system theme-color when localStorage is empty or denied', () => {
		const fromEmptyStorage = executeBootstrap({
			systemDark: false,
			inlineThemes: ['sahara'],
			initialCookies: { 'bookward-theme': 'sahara', 'bookward-color-mode': 'system' },
		});
		expect(fromEmptyStorage.dataset.theme).toBe('sahara');
		expect(fromEmptyStorage.dataset.mode).toBe('system');
		expect(fromEmptyStorage.dataset.colorScheme).toBe('light');
		expect(fromEmptyStorage.metaThemeColor).toBe(THEME_COLORS.sahara.light);
		expect(fromEmptyStorage.cookieWrites).toHaveLength(0);

		const fromDeniedStorage = executeBootstrap({
			denyStorage: true,
			systemDark: true,
			inlineThemes: ['sahara'],
			initialCookies: { 'bookward-theme': 'sahara', 'bookward-color-mode': 'system' },
		});
		expect(fromDeniedStorage.dataset.theme).toBe('sahara');
		expect(fromDeniedStorage.dataset.colorScheme).toBe('dark');
		expect(fromDeniedStorage.metaThemeColor).toBe(THEME_COLORS.sahara.dark);
	});

	it('falls back to the default theme and dark mode for invalid saved values or cookies', () => {
		const root = executeBootstrap({
			systemDark: false,
			readStorage: () => 'not-a-real-preference',
			initialCookies: { 'bookward-theme': 'invalid', 'bookward-color-mode': 'sepia' },
		});

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
		expect(root.metaThemeColor).toBe(THEME_COLORS.cerberus.dark);

		const invalidCookiesOnly = executeBootstrap({
			systemDark: false,
			initialCookies: { 'bookward-theme': 'invalid', 'bookward-color-mode': 'sepia' },
		});
		expect(invalidCookiesOnly.dataset.theme).toBe('cerberus');
		expect(invalidCookiesOnly.dataset.mode).toBe('dark');
		expect(invalidCookiesOnly.metaThemeColor).toBe(THEME_COLORS.cerberus.dark);
	});

	it('falls back to dark mode when localStorage is denied', () => {
		const root = executeBootstrap({ denyStorage: true, systemDark: false });

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.style.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
		expect(root.metaThemeColor).toBe(THEME_COLORS.cerberus.dark);
	});

	it('keeps a dark fallback when matchMedia is unavailable', () => {
		const root = executeBootstrap({ denyStorage: true, withoutMatchMedia: true, systemDark: true });

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.style.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
		expect(root.metaThemeColor).toBe(THEME_COLORS.cerberus.dark);
	});
});
