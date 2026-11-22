import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	DEFAULT_MODE,
	DEFAULT_THEME,
	MODE_STORAGE_KEY,
	THEME_COLORS,
	THEME_LOADERS,
	THEME_SWATCHES,
	THEME_STORAGE_KEY,
	THEMES,
	applyTheme,
	getStoredPreferences,
	getThemeColor,
	initAppearance,
	isThemeMode,
	isThemeName,
	loadTheme,
	persistPreferences,
	resolveMode,
	setTheme,
	type ThemeMode,
} from './theme';
import { load as loadRootLayout } from '../routes/+layout.server';

type MemoryStorage = {
	getItem: (key: string) => string | null;
	setItem: (key: string, value: string) => void;
};

type FakeRoot = {
	dataset: Record<string, string>;
	style: { colorScheme: string };
	classList: { toggle: (name: string, force?: boolean) => boolean };
};

function oklchToHex(input: string): string {
	const match = input.trim().match(/^oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+(none|[\d.]+)(?:deg)?(?:\s*\/\s*[\d.%]+)?\s*\)$/i);
	if (!match) throw new Error(`Unsupported OKLCH token: ${input}`);
	const lightness = match[2] === '%' ? Number.parseFloat(match[1]) / 100 : Number.parseFloat(match[1]);
	const chroma = Number.parseFloat(match[3]);
	const hue = match[4] === 'none' ? 0 : (Number.parseFloat(match[4]) * Math.PI) / 180;
	const a = chroma * Math.cos(hue);
	const b = chroma * Math.sin(hue);

	const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
	const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
	const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;

	const linear = [
		4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
		-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
		-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
	];

	return `#${linear
		.map((channel) => {
			const clamped = Math.min(1, Math.max(0, channel));
			const srgb = clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * clamped ** (1 / 2.4) - 0.055;
			return Math.round(srgb * 255).toString(16).padStart(2, '0');
		})
		.join('')}`;
}

function resolveCssToken(source: string, token: string): string {
	let current = source.match(new RegExp(`${token}:\\s*([^;]+)`))?.[1]?.trim() ?? '';
	while (current.startsWith('var(')) {
		const referenced = current.slice(4, -1).trim();
		current = source.match(new RegExp(`${referenced}:\\s*([^;]+)`))?.[1]?.trim() ?? '';
	}
	return current;
}

function createCookieJar() {
	const entries = new Map<string, string>();
	const writes: string[] = [];
	return {
		entries,
		writes,
		get cookie() {
			return [...entries.entries()].map(([key, value]) => `${key}=${value}`).join('; ');
		},
		set cookie(raw: string) {
			writes.push(raw);
			const [pair] = raw.split(';');
			const eq = pair.indexOf('=');
			if (eq >= 0) entries.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
		},
	};
}

function createStorage(): MemoryStorage & { values: Map<string, string> } {
	const values = new Map<string, string>();
	return {
		values,
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value),
	};
}

function createRoot(): FakeRoot & { classes: Set<string> } {
	const classes = new Set<string>();
	return {
		classes,
		dataset: {},
		style: { colorScheme: '' },
		classList: {
			toggle: (name, force) => {
				const enabled = force ?? !classes.has(name);
				if (enabled) classes.add(name);
				else classes.delete(name);
				return enabled;
			},
		},
	};
}

describe('theme preferences', () => {
	let storage: ReturnType<typeof createStorage>;
	let root: ReturnType<typeof createRoot>;
	let systemDark = false;

	beforeEach(() => {
		storage = createStorage();
		root = createRoot();
		systemDark = false;
		vi.stubGlobal('window', {
			localStorage: storage,
			matchMedia: vi.fn(() => ({ matches: systemDark })),
		});
		vi.stubGlobal('document', { documentElement: root });
	});

	afterEach(() => vi.unstubAllGlobals());

	it('keeps the picker catalog in sync with every installed Skeleton theme', () => {
		const installed = readdirSync(join(process.cwd(), 'node_modules/@skeletonlabs/skeleton/src/themes'))
			.filter((file) => file.endsWith('.css'))
			.map((file) => file.replace(/\.css$/, ''))
			.sort();
		const pickerThemes = THEMES.map((theme) => theme.id).sort();

		expect(pickerThemes).toEqual(installed);
		expect(new Set(pickerThemes).size).toBe(pickerThemes.length);

		const stylesheet = readFileSync(join(process.cwd(), 'src/routes/layout.css'), 'utf8');
		const appHtml = readFileSync(join(process.cwd(), 'src/app.html'), 'utf8');
		expect(stylesheet).toContain(`@import '@skeletonlabs/skeleton/themes/${DEFAULT_THEME}'`);
		expect(appHtml.indexOf('<script>')).toBeGreaterThan(appHtml.indexOf('%sveltekit.head%'));
		for (const theme of pickerThemes) expect(THEME_LOADERS[theme]).toEqual(expect.any(Function));
		for (const theme of pickerThemes) {
			const source = readFileSync(join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme}.css`), 'utf8');
			for (const color of ['primary', 'secondary', 'tertiary'] as const) {
				const value = source.match(new RegExp(`--color-${color}-500:\\s*([^;]+)`))?.[1]?.trim();
				expect(value).toBe(THEME_SWATCHES[theme][color]);
			}
			const expectedLight = oklchToHex(resolveCssToken(source, '--color-root-bg-light'));
			const expectedDark = oklchToHex(resolveCssToken(source, '--color-root-bg-dark'));
			expect(THEME_COLORS[theme]).toEqual({ light: expectedLight, dark: expectedDark });
			expect(appHtml).toContain(`${theme}: { light: '${expectedLight}', dark: '${expectedDark}' }`);
		}
		for (const theme of pickerThemes.filter((theme) => theme !== DEFAULT_THEME)) {
			expect(stylesheet).not.toContain(`@import '@skeletonlabs/skeleton/themes/${theme}'`);
		}
	});

	it('accepts every installed theme and only the supported color modes', () => {
		for (const theme of THEMES) expect(isThemeName(theme.id)).toBe(true);
		expect(isThemeName('not-installed')).toBe(false);
		expect(isThemeName(null)).toBe(false);
		for (const mode of ['light', 'dark', 'system'] satisfies ThemeMode[]) expect(isThemeMode(mode)).toBe(true);
		expect(isThemeMode('auto')).toBe(false);
	});

	it('injects an alternate stylesheet only once before it is selected', async () => {
		const originalLoader = THEME_LOADERS.mint;
		const append = vi.fn();
		const style = { dataset: {} as DOMStringMap, textContent: '' };
		THEME_LOADERS.mint = () => Promise.resolve({ default: '[data-theme="mint"] {}' });
		vi.stubGlobal('document', {
			head: { querySelector: vi.fn(() => null), append },
			createElement: vi.fn(() => style),
		});

		await loadTheme('mint');
		await loadTheme('mint');

		expect(style.dataset.bookwardTheme).toBe('mint');
		expect(style.textContent).toContain('[data-theme="mint"]');
		expect(append).toHaveBeenCalledTimes(1);
		THEME_LOADERS.mint = originalLoader;
	});

	it('short-circuits loadTheme without downloading CSS when SSR already inlined the style tag', async () => {
		const originalLoader = THEME_LOADERS.sahara;
		const loaderSpy = vi.fn(() => Promise.resolve({ default: '[data-theme="sahara"] {}' }));
		const append = vi.fn();
		THEME_LOADERS.sahara = loaderSpy;
		vi.stubGlobal('document', {
			head: {
				querySelector: vi.fn((selector: string) =>
					selector === 'style[data-bookward-theme="sahara"]' ? { dataset: { bookwardTheme: 'sahara' } } : null,
				),
				append,
			},
			createElement: vi.fn(),
		});

		try {
			await loadTheme('sahara');
			expect(loaderSpy).not.toHaveBeenCalled();
			expect(append).not.toHaveBeenCalled();
		} finally {
			THEME_LOADERS.sahara = originalLoader;
		}
	});

	it('writes SameSite=Lax cookies in persistPreferences and updates meta theme-color in applyTheme', () => {
		const jar = createCookieJar();
		const meta = { content: '#000000', setAttribute: vi.fn() };
		const doc: Record<string, unknown> = {
			documentElement: root,
			querySelector: vi.fn((selector: string) => (selector === 'meta[name="theme-color"]' ? meta : null)),
		};
		Object.defineProperty(doc, 'cookie', {
			configurable: true,
			get: () => jar.cookie,
			set: (value: string) => {
				jar.cookie = value;
			},
		});
		vi.stubGlobal('document', doc);
		vi.stubGlobal('window', {
			localStorage: storage,
			location: { protocol: 'https:' },
			matchMedia: vi.fn(() => ({ matches: systemDark })),
		});

		persistPreferences('sahara', 'light');
		expect(jar.entries.get(THEME_STORAGE_KEY)).toBe('sahara');
		expect(jar.entries.get(MODE_STORAGE_KEY)).toBe('light');
		expect(jar.writes).toEqual([
			`${THEME_STORAGE_KEY}=sahara; Path=/; Max-Age=31536000; SameSite=Lax; Secure`,
			`${MODE_STORAGE_KEY}=light; Path=/; Max-Age=31536000; SameSite=Lax; Secure`,
		]);

		applyTheme('sahara', 'light');
		expect(meta.content).toBe(THEME_COLORS.sahara.light);
		expect(meta.setAttribute).toHaveBeenCalledWith('content', THEME_COLORS.sahara.light);

		systemDark = true;
		applyTheme('sahara', 'system');
		expect(meta.content).toBe(THEME_COLORS.sahara.dark);
		expect(getThemeColor('sahara', 'dark')).toBe(THEME_COLORS.sahara.dark);

		storage.values.clear();
		expect(getStoredPreferences()).toEqual({ theme: 'sahara', mode: 'light' });
	});

	it('inlines only active non-default theme CSS and resolves theme-color during root layout SSR', async () => {
		const originalSaharaLoader = THEME_LOADERS.sahara;
		const saharaCss = readFileSync(join(process.cwd(), 'node_modules/@skeletonlabs/skeleton/src/themes/sahara.css'), 'utf8');
		THEME_LOADERS.sahara = () => Promise.resolve({ default: saharaCss });

		try {
			const defaultResult = await loadRootLayout({
				locals: {},
				cookies: { get: () => undefined },
			} as never);
			expect(defaultResult).toEqual({
				user: null,
				theme: 'cerberus',
				mode: 'dark',
				themeCss: null,
				themeColor: THEME_COLORS.cerberus.dark,
			});

			const saharaResult = await loadRootLayout({
				locals: { user: { id: 'u1', profile_id: 'p1', username: 'reader', display_name: 'Reader', role: 'user', must_change_password: false } },
				cookies: {
					get: (name: string) => {
						if (name === THEME_STORAGE_KEY) return 'sahara';
						if (name === MODE_STORAGE_KEY) return 'light';
						return undefined;
					},
				},
			} as never);
			expect(saharaResult).toMatchObject({
				theme: 'sahara',
				mode: 'light',
				themeColor: THEME_COLORS.sahara.light,
				themeCss: expect.stringContaining(':root[data-theme="sahara"]'),
			});

			const invalidCookieResult = await loadRootLayout({
				locals: {},
				cookies: {
					get: (name: string) => (name === THEME_STORAGE_KEY ? 'unknown-theme' : 'sepia'),
				},
			} as never);
			expect(invalidCookieResult).toEqual({
				user: null,
				theme: 'cerberus',
				mode: 'dark',
				themeCss: null,
				themeColor: THEME_COLORS.cerberus.dark,
			});

			THEME_LOADERS.sahara = () => Promise.reject(new Error('unavailable'));
			const failedLoaderResult = await loadRootLayout({
				locals: {},
				cookies: {
					get: (name: string) => (name === THEME_STORAGE_KEY ? 'sahara' : 'dark'),
				},
			} as never);
			expect(failedLoaderResult).toEqual({
				user: null,
				theme: 'cerberus',
				mode: 'dark',
				themeCss: null,
				themeColor: THEME_COLORS.cerberus.dark,
			});
		} finally {
			THEME_LOADERS.sahara = originalSaharaLoader;
		}
	});

	it('falls back safely when storage contains stale or invalid values', () => {
		storage.values.set(THEME_STORAGE_KEY, 'a-theme-from-an-old-install');
		storage.values.set(MODE_STORAGE_KEY, 'sepia');

		expect(getStoredPreferences()).toEqual({ theme: DEFAULT_THEME, mode: DEFAULT_MODE });
	});

	it('reads and writes valid theme and mode preferences independently', () => {
		storage.values.set(THEME_STORAGE_KEY, 'rosepine');
		storage.values.set(MODE_STORAGE_KEY, 'light');
		expect(getStoredPreferences()).toEqual({ theme: 'rosepine', mode: 'light' });

		setTheme('terminus', 'dark');
		expect(storage.values.get(THEME_STORAGE_KEY)).toBe('terminus');
		expect(storage.values.get(MODE_STORAGE_KEY)).toBe('dark');
		expect(root.dataset.theme).toBe('terminus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.style.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
	});

	it('resolves system mode again when the operating system preference changes', () => {
		expect(resolveMode('system')).toBe('light');
		systemDark = true;
		expect(resolveMode('system')).toBe('dark');

		applyTheme('cerberus', 'system');
		expect(root.dataset.mode).toBe('system');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
	});

	it('falls back to light when matchMedia is unavailable or restricted', () => {
		vi.stubGlobal('window', { localStorage: storage });
		expect(resolveMode('system')).toBe('light');
		expect(() => applyTheme('mint', 'system')).not.toThrow();
		expect(root.dataset.colorScheme).toBe('light');

		vi.stubGlobal('window', {
			localStorage: storage,
			matchMedia: vi.fn(() => {
				throw new Error('matchMedia denied');
			}),
		});
		expect(resolveMode('system')).toBe('light');
	});

	it('loads the saved palette before applying it and re-applies system mode on OS changes', async () => {
		const originalNoshLoader = THEME_LOADERS.nosh;
		const originalRoseLoader = THEME_LOADERS.rose;
		THEME_LOADERS.nosh = () => Promise.resolve({ default: '[data-theme="nosh"] {}' });
		THEME_LOADERS.rose = () => Promise.resolve({ default: '[data-theme="rose"] {}' });
		const appended: Array<{ dataset: Record<string, string>; textContent: string }> = [];
		vi.stubGlobal('document', {
			documentElement: root,
			head: {
				querySelector: vi.fn(() => null),
				append: vi.fn((style: { dataset: Record<string, string>; textContent: string }) => appended.push(style)),
			},
			createElement: vi.fn(() => ({ dataset: {}, textContent: '' })),
		});
		storage.values.set(THEME_STORAGE_KEY, 'nosh');
		storage.values.set(MODE_STORAGE_KEY, 'system');
		let mediaChange: (() => void) | undefined;
		const removeMediaListener = vi.fn();
		const removeStorageListener = vi.fn();
		const media = {
			get matches() {
				return systemDark;
			},
			addEventListener: vi.fn((_event: string, listener: () => void) => {
				mediaChange = listener;
			}),
			removeEventListener: removeMediaListener,
		};
		const storageListener = vi.fn();
		vi.stubGlobal('window', {
			localStorage: storage,
			matchMedia: vi.fn(() => media),
			addEventListener: vi.fn((_event: string, listener: () => void) => storageListener.mockImplementation(listener)),
			removeEventListener: removeStorageListener,
		});

		const cleanup = initAppearance();
		try {
			await new Promise<void>((resolve) => setTimeout(resolve, 0));
			expect(root.dataset.theme).toBe('nosh');
			expect(root.dataset.colorScheme).toBe('light');
			expect(appended).toHaveLength(1);
			expect(appended[0].dataset.bookwardTheme).toBe('nosh');

			systemDark = true;
			mediaChange?.();
			await new Promise<void>((resolve) => setTimeout(resolve, 0));
			expect(root.dataset.colorScheme).toBe('dark');
			expect(root.classes.has('dark')).toBe(true);

			storage.values.set(THEME_STORAGE_KEY, 'rose');
			storageListener();
			await new Promise<void>((resolve) => setTimeout(resolve, 0));
			expect(root.dataset.theme).toBe('rose');
			expect(appended).toHaveLength(2);

			expect(removeMediaListener).not.toHaveBeenCalled();
		} finally {
			cleanup();
			THEME_LOADERS.nosh = originalNoshLoader;
			THEME_LOADERS.rose = originalRoseLoader;
		}

		expect(removeMediaListener).toHaveBeenCalledWith('change', expect.any(Function));
		expect(removeStorageListener).toHaveBeenCalledWith('storage', expect.any(Function));
	});

	it('remains usable when browser storage is unavailable', () => {
		vi.stubGlobal('window', {
			matchMedia: vi.fn(() => ({ matches: false })),
		});

		expect(getStoredPreferences()).toEqual({ theme: DEFAULT_THEME, mode: DEFAULT_MODE });
		expect(() => setTheme('mint', 'light')).not.toThrow();
		expect(root.dataset.theme).toBe('mint');
		expect(root.dataset.colorScheme).toBe('light');
	});
});
