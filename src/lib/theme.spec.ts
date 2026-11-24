import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { compile } from '@tailwindcss/node';
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
	loadThemeCss,
	resolveMode,
	setTheme,
	type ThemeMode,
} from './theme';
import {
	DEFAULT_SKELETON_ROOT_TOKENS,
	THEME_COLOR_ROLES,
	THEME_SHADES,
	buildCriticalCalibrationCss,
	calibrateThemeTokens,
	contrastRatio,
	extractCssVariables,
	mixOklab,
	mixSrgbAlpha,
	oklchToHex,
	parseCssColor,
	parseEmittedThemeStylesheetVars,
	resolveCalibratedThemeStylesheetVars,
} from './server/theme-contrast';

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

	it('keeps the picker catalog, calibrated THEME_COLORS pins, and client bundle boundary in sync', async () => {
		const installed = readdirSync(join(process.cwd(), 'node_modules/@skeletonlabs/skeleton/src/themes'))
			.filter((file) => file.endsWith('.css'))
			.map((file) => file.replace(/\.css$/, ''))
			.sort();
		const pickerThemes = THEMES.map((theme) => theme.id).sort();

		expect(pickerThemes).toEqual(installed);
		expect(new Set(pickerThemes).size).toBe(pickerThemes.length);

		const themeModuleSource = readFileSync(join(process.cwd(), 'src/lib/theme.ts'), 'utf8');
		expect(themeModuleSource).not.toContain('theme-contrast');
		expect(themeModuleSource).not.toContain('calibrateThemeStylesheet');

		const stylesheetPath = join(process.cwd(), 'src/routes/layout.css');
		const stylesheet = readFileSync(stylesheetPath, 'utf8');
		expect(stylesheet).toContain(`@import '@skeletonlabs/skeleton/themes/${DEFAULT_THEME}'`);
		expect(stylesheet).toContain(`@import './theme-calibration.css'`);
		expect(stylesheet).not.toContain('@skeletonlabs/skeleton-svelte');
		const cerberusCss = readFileSync(
			join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${DEFAULT_THEME}.css`),
			'utf8',
		);
		const criticalCalibration = readFileSync(
			join(process.cwd(), 'src/routes/theme-calibration.css'),
			'utf8',
		);
		expect(criticalCalibration).toBe(buildCriticalCalibrationCss(cerberusCss));
		for (const theme of pickerThemes) expect(THEME_LOADERS[theme]).toEqual(expect.any(Function));
		for (const theme of pickerThemes) {
			const source = readFileSync(
				join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme}.css`),
				'utf8',
			);
			for (const color of ['primary', 'secondary', 'tertiary'] as const) {
				const value = source.match(new RegExp(`--color-${color}-500:\\s*([^;]+)`))?.[1]?.trim();
				expect(value).toBe(THEME_SWATCHES[theme][color]);
			}

			const calibrated = calibrateThemeTokens(extractCssVariables(source));
			const expectedLight = oklchToHex(calibrated.light['--color-root-bg-light']);
			const expectedDark = oklchToHex(calibrated.light['--color-root-bg-dark']);
			expect(THEME_COLORS[theme]).toEqual({ light: expectedLight, dark: expectedDark });
			expect(getThemeColor(theme, 'light')).toBe(expectedLight);
			expect(getThemeColor(theme, 'dark')).toBe(expectedDark);
		}
		expect(getThemeColor('unknown-theme' as never, 'dark')).toBe(THEME_COLORS[DEFAULT_THEME].dark);
		for (const theme of pickerThemes.filter((theme) => theme !== DEFAULT_THEME)) {
			expect(stylesheet).not.toContain(`@import '@skeletonlabs/skeleton/themes/${theme}'`);
			expect(criticalCalibration).not.toContain(`:root[data-theme="${theme}"]`);
		}

		const compiler = await compile(stylesheet, {
			base: dirname(stylesheetPath),
			from: stylesheetPath,
			onDependency: () => undefined,
		});
		const compiled = compiler.build([]);
		expect(compiled).toContain(`[data-theme="${DEFAULT_THEME}"]`);
		expect(compiled).not.toContain('[data-scope=');
	});

	it('calibrates every Skeleton palette and :root fallback to WCAG 2.2 G18 4.5:1 across solid and translucent mixes (10%-30%)', () => {
		const palettes = [
			{ id: 'root', raw: DEFAULT_SKELETON_ROOT_TOKENS },
			...THEMES.map((theme) => ({
				id: theme.id,
				raw: extractCssVariables(
					readFileSync(
						join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme.id}.css`),
						'utf8',
					),
				),
			})),
		];

		for (const palette of palettes) {
			const calibrated = calibrateThemeTokens(palette.raw);

			// Surface scale must be strictly monotonic from 50 (lightest) to 950 (darkest).
			const surfaceLightness = THEME_SHADES.map((shade) => {
				const token = calibrated.light[`--color-surface-${shade}`];
				expect(token, `${palette.id} --color-surface-${shade}`).toBeDefined();
				return parseCssColor(token).L;
			});
			for (let i = 0; i < surfaceLightness.length - 1; i += 1) {
				expect(
					surfaceLightness[i],
					`${palette.id} surface-${THEME_SHADES[i]} > surface-${THEME_SHADES[i + 1]}`,
				).toBeGreaterThan(surfaceLightness[i + 1]);
			}

			for (const mode of ['light', 'dark'] as const) {
				const active = mode === 'dark' ? { ...calibrated.light, ...calibrated.dark } : calibrated.light;
				const surface50 = parseCssColor(active['--color-surface-50']);
				const surface950 = parseCssColor(active['--color-surface-950']);
				const surfaceBg = parseCssColor(active['--color-surface-50-950']);
				const oppositeSurfaceBg = parseCssColor(active['--color-surface-950-50']);
				const rootBg = parseCssColor(
					active[mode === 'light' ? '--color-root-bg-light' : '--color-root-bg-dark'],
				);
				const primary500 = parseCssColor(active['--color-primary-500']);
				const secondary500 = parseCssColor(active['--color-secondary-500']);
				const tertiary500 = parseCssColor(active['--color-tertiary-500']);

				// Assert single-shade and dual-shade surface contrast tokens in both light and dark modes
				// (specifically guarding dark-mode `--color-surface-contrast-50` inheritance).
				expect(
					contrastRatio(parseCssColor(active['--color-surface-contrast-50']), surface50),
					`${palette.id}/${mode} --color-surface-contrast-50 on surface-50`,
				).toBeGreaterThanOrEqual(4.5);
				expect(
					contrastRatio(parseCssColor(active['--color-surface-contrast-950']), surface950),
					`${palette.id}/${mode} --color-surface-contrast-950 on surface-950`,
				).toBeGreaterThanOrEqual(4.5);
				expect(
					contrastRatio(parseCssColor(active['--color-surface-contrast-50-950']), surfaceBg),
					`${palette.id}/${mode} --color-surface-contrast-50-950 on surface-50-950`,
				).toBeGreaterThanOrEqual(4.5);
				expect(
					contrastRatio(
						parseCssColor(active['--color-surface-contrast-950-50']),
						oppositeSurfaceBg,
					),
					`${palette.id}/${mode} --color-surface-contrast-950-50 on surface-950-50`,
				).toBeGreaterThanOrEqual(4.5);

				// Solid surfaces + real translucent card/modal/callout/login mixes beyond 5-8%:
				const backgroundMixes: Array<[string, ReturnType<typeof parseCssColor>]> = [
					['surface-50-950', surfaceBg],
					['root-bg', rootBg],
					['preset-tonal (10% oklab)', mixOklab(surfaceBg, oppositeSurfaceBg, 0.1)],
					['bg-surface-950-50/10 (srgb)', mixSrgbAlpha(surfaceBg, oppositeSurfaceBg, 0.1)],
					['bg-primary-500/15', mixSrgbAlpha(surfaceBg, primary500, 0.15)],
					['bg-secondary-500/15', mixSrgbAlpha(surfaceBg, secondary500, 0.15)],
					['bg-tertiary-500/15', mixSrgbAlpha(surfaceBg, tertiary500, 0.15)],
					['bg-primary-500/20', mixSrgbAlpha(surfaceBg, primary500, 0.2)],
					['bg-secondary-500/20', mixSrgbAlpha(surfaceBg, secondary500, 0.2)],
					['bg-tertiary-500/20', mixSrgbAlpha(surfaceBg, tertiary500, 0.2)],
					[
						'stacked mobile card (8% primary + 15% secondary)',
						mixSrgbAlpha(mixSrgbAlpha(surfaceBg, primary500, 0.08), secondary500, 0.15),
					],
					[
						'stacked mobile card (8% secondary + 15% primary)',
						mixSrgbAlpha(mixSrgbAlpha(surfaceBg, secondary500, 0.08), primary500, 0.15),
					],
					['login radial glow (30% primary oklab)', mixOklab(surfaceBg, primary500, 0.3)],
					['login radial glow (26% secondary oklab)', mixOklab(surfaceBg, secondary500, 0.26)],
					['login radial glow (23% tertiary oklab)', mixOklab(surfaceBg, tertiary500, 0.23)],
				];

				const textProps = [
					'--color-surface-contrast-50-950',
					'--color-surface-600-400',
					'--color-surface-700-300',
					'--color-surface-800-200',
					'--color-surface-900-100',
					'--color-surface-950-50',
					mode === 'light' ? '--typo-base--color-light' : '--typo-base--color-dark',
					mode === 'light' ? '--typo-anchor--color-light' : '--typo-anchor--color-dark',
				];
				const headingProp =
					mode === 'light' ? '--typo-heading--color-light' : '--typo-heading--color-dark';
				if (active[headingProp]) textProps.push(headingProp);

				for (const textProp of textProps) {
					const fg = parseCssColor(active[textProp]);
					for (const [mixLabel, bg] of backgroundMixes) {
						expect(
							contrastRatio(fg, bg),
							`${palette.id}/${mode} ${textProp} on ${mixLabel}`,
						).toBeGreaterThanOrEqual(4.5);
					}
				}

				// Underlying 600 (light) and 400 (dark) tokens inlined by Tailwind v4 utilities,
				// paired 600-400 tokens, tonal presets (+ 10-15% overlays), filled-500 presets, and switches.
				for (const role of THEME_COLOR_ROLES) {
					const shadeProp = `--color-${role}-${mode === 'light' ? '600' : '400'}`;
					const shadeFg = parseCssColor(active[shadeProp]);
					const paired600400 = parseCssColor(active[`--color-${role}-600-400`]);
					for (const [mixLabel, bg] of backgroundMixes) {
						expect(
							contrastRatio(shadeFg, bg),
							`${palette.id}/${mode} ${shadeProp} on ${mixLabel}`,
						).toBeGreaterThanOrEqual(4.5);
						expect(
							contrastRatio(paired600400, bg),
							`${palette.id}/${mode} --color-${role}-600-400 on ${mixLabel}`,
						).toBeGreaterThanOrEqual(4.5);
					}

					const tonalBg = parseCssColor(active[`--color-${role}-50-950`]);
					const tonalFg = parseCssColor(active[`--color-${role}-950-50`]);
					const tonalMixes: Array<[string, ReturnType<typeof parseCssColor>]> = [
						['solid tonal', tonalBg],
						['tonal + 15% primary-500', mixSrgbAlpha(tonalBg, primary500, 0.15)],
						['tonal + 15% secondary-500', mixSrgbAlpha(tonalBg, secondary500, 0.15)],
						['tonal + 10% surface-950-50', mixSrgbAlpha(tonalBg, oppositeSurfaceBg, 0.1)],
					];
					for (const [tonalLabel, mixedTonalBg] of tonalMixes) {
						expect(
							contrastRatio(tonalFg, mixedTonalBg),
							`${palette.id}/${mode} preset-tonal-${role} on ${tonalLabel}`,
						).toBeGreaterThanOrEqual(4.5);
					}

					const filledBg = parseCssColor(active[`--color-${role}-500`]);
					const filledFg = parseCssColor(active[`--color-${role}-contrast-500`]);
					expect(
						contrastRatio(filledFg, filledBg),
						`${palette.id}/${mode} preset-filled-${role}-500`,
					).toBeGreaterThanOrEqual(4.5);

					if (role === 'primary' || role === 'secondary' || role === 'tertiary' || role === 'surface') {
						expect(
							contrastRatio(filledBg, surfaceBg),
							`${palette.id}/${mode} switch ${role}-500 vs surface-50-950`,
						).toBeGreaterThanOrEqual(3);
					}
				}
			}
		}
	});

	it('serves build-time calibrated and selector-elevated CSS through loadThemeCss in SSR without DOM globals', async () => {
		vi.unstubAllGlobals();
		expect(typeof document).toBe('undefined');
		expect(await loadThemeCss(DEFAULT_THEME)).toBeNull();
		expect(await loadThemeCss('not-a-theme' as never)).toBeNull();

		for (const theme of THEMES) {
			if (theme.id === DEFAULT_THEME) continue;
			const ssrCss = await loadThemeCss(theme.id);
			expect(ssrCss, `SSR CSS for ${theme.id}`).toBeTruthy();
			expect(ssrCss).toContain(`:root[data-theme="${theme.id}"], [data-theme="${theme.id}"]`);
			expect(ssrCss).toContain(`:root[data-theme="${theme.id}"] {`);
			expect(ssrCss).toContain(`:root[data-theme="${theme.id}"].dark {`);

			const rawCss = readFileSync(
				join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme.id}.css`),
				'utf8',
			);
			for (const mode of ['light', 'dark'] as const) {
				const emittedVars = parseEmittedThemeStylesheetVars(ssrCss!, mode);
				const expectedVars = resolveCalibratedThemeStylesheetVars(rawCss, mode);
				expect(emittedVars['--color-root-bg-light']).toBe(expectedVars['--color-root-bg-light']);
				expect(emittedVars['--color-root-bg-dark']).toBe(expectedVars['--color-root-bg-dark']);
				expect(emittedVars['--color-surface-50-950']).toBe(expectedVars['--color-surface-50-950']);
				expect(emittedVars['--color-primary-500']).toBe(expectedVars['--color-primary-500']);
				expect(emittedVars['--color-primary-600-400']).toBe(expectedVars['--color-primary-600-400']);
			}
		}
	});

	it('short-circuits loadTheme without downloading CSS when SSR already inlined the style tag', async () => {
		const originalLoader = THEME_LOADERS.sahara;
		const loaderSpy = vi.fn(() => Promise.resolve({ default: '[data-theme="sahara"] {}' }));
		const append = vi.fn();
		THEME_LOADERS.sahara = loaderSpy;
		vi.stubGlobal('document', {
			head: {
				querySelector: vi.fn((selector: string) =>
					selector === 'style[data-bookward-theme="sahara"]'
						? { dataset: { bookwardTheme: 'sahara' } }
						: null,
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

		try {
			await loadTheme('mint');
			await loadTheme('mint');

			expect(style.dataset.bookwardTheme).toBe('mint');
			expect(style.textContent).toContain('[data-theme="mint"]');
			expect(style.textContent).toContain(':root[data-theme="mint"]');
			expect(append).toHaveBeenCalledTimes(1);
		} finally {
			THEME_LOADERS.mint = originalLoader;
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
