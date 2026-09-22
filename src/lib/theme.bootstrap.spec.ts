import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

type BootstrapRoot = {
	dataset: Record<string, string>;
	style: { colorScheme: string };
	classes: Set<string>;
	classList: { toggle: (name: string, force?: boolean) => boolean };
};

const appHtml = readFileSync(join(process.cwd(), 'src/app.html'), 'utf8');
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
}): BootstrapRoot {
	if (!bootstrap) throw new Error('app.html is missing the appearance bootstrap');

	const classes = new Set<string>();
	const root: BootstrapRoot = {
		dataset: { theme: 'cerberus', mode: 'dark', colorScheme: 'dark' },
		style: { colorScheme: '' },
		classes,
		classList: {
			toggle: (name, force) => {
				const enabled = force ?? !classes.has(name);
				if (enabled) classes.add(name);
				else classes.delete(name);
				return enabled;
			},
		},
	};

	const windowObject: { localStorage?: unknown; matchMedia?: () => { matches: boolean } } = {};
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
		document: { documentElement: root },
	});
	return root;
}

describe('no-flash appearance bootstrap', () => {
	it.each([
		{ mode: 'dark', systemDark: false, resolved: 'dark', hasDarkClass: true },
		{ mode: 'light', systemDark: true, resolved: 'light', hasDarkClass: false },
		{ mode: 'system', systemDark: true, resolved: 'dark', hasDarkClass: true },
	] as const)('applies saved $mode preferences before hydration', ({ mode, systemDark, resolved, hasDarkClass }) => {
		const root = executeBootstrap({
			systemDark,
			readStorage: (key) => (key === 'afterword-theme' ? 'rocket' : mode),
		});

		expect(root.dataset.theme).toBe('rocket');
		expect(root.dataset.mode).toBe(mode);
		expect(root.dataset.colorScheme).toBe(resolved);
		expect(root.style.colorScheme).toBe(resolved);
		expect(root.classes.has('dark')).toBe(hasDarkClass);
	});

	it('falls back to the default theme and dark mode for invalid saved values', () => {
		const root = executeBootstrap({
			systemDark: false,
			readStorage: () => 'not-a-real-preference',
		});

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
	});

	it('falls back to dark mode when localStorage is denied', () => {
		const root = executeBootstrap({ denyStorage: true, systemDark: false });

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.style.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
	});

	it('keeps a dark fallback when matchMedia is unavailable', () => {
		const root = executeBootstrap({ denyStorage: true, withoutMatchMedia: true, systemDark: true });

		expect(root.dataset.theme).toBe('cerberus');
		expect(root.dataset.mode).toBe('dark');
		expect(root.dataset.colorScheme).toBe('dark');
		expect(root.style.colorScheme).toBe('dark');
		expect(root.classes.has('dark')).toBe(true);
	});
});
