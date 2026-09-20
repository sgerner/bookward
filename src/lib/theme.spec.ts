import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
	import {
	DEFAULT_MODE,
	DEFAULT_THEME,
	MODE_STORAGE_KEY,
	THEME_LOADERS,
	THEME_SWATCHES,
	THEME_STORAGE_KEY,
	THEMES,
	applyTheme,
	getStoredPreferences,
	initAppearance,
	isThemeMode,
	isThemeName,
	loadTheme,
	resolveMode,
	setTheme,
	type ThemeMode,
} from './theme';

type MemoryStorage = {
	getItem: (key: string) => string | null;
	setItem: (key: string, value: string) => void;
};

type FakeRoot = {
	dataset: Record<string, string>;
	style: { colorScheme: string };
	classList: { toggle: (name: string, force?: boolean) => boolean };
};

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
		expect(stylesheet).toContain(`@import '@skeletonlabs/skeleton/themes/${DEFAULT_THEME}'`);
		for (const theme of pickerThemes) expect(THEME_LOADERS[theme]).toEqual(expect.any(Function));
		for (const theme of pickerThemes) {
			const source = readFileSync(join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme}.css`), 'utf8');
			for (const color of ['primary', 'secondary', 'tertiary'] as const) {
				const value = source.match(new RegExp(`--color-${color}-500:\\s*([^;]+)`))?.[1]?.trim();
				expect(value).toBe(THEME_SWATCHES[theme][color]);
			}
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

	it('re-applies system mode on OS changes and cleans up listeners', () => {
		storage.values.set(THEME_STORAGE_KEY, 'mint');
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
		expect(root.dataset.theme).toBe('mint');
		expect(root.dataset.colorScheme).toBe('light');

		systemDark = true;
		mediaChange?.();
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);

		storage.values.set(THEME_STORAGE_KEY, 'rose');
		storageListener();
		expect(root.dataset.theme).toBe('rose');

		cleanup();
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
