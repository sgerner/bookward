import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { THEMES, type SkeletonThemeName } from './theme';

type Oklab = { L: number; a: number; b: number };
type Mode = 'light' | 'dark';

const WHITE: Oklab = { L: 1, a: 0, b: 0 };
const BLACK: Oklab = { L: 0, a: 0, b: 0 };

function parseHexToOklab(hex: string): Oklab {
	const normalized = hex.replace('#', '');
	const r = parseInt(normalized.slice(0, 2), 16) / 255;
	const g = parseInt(normalized.slice(2, 4), 16) / 255;
	const b = parseInt(normalized.slice(4, 6), 16) / 255;
	const toLin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
	const lr = toLin(r);
	const lg = toLin(g);
	const lb = toLin(b);
	const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
	const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
	const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
	return {
		L: 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
		a: 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
		b: 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
	};
}

function splitTopLevelArgs(input: string): string[] {
	const parts: string[] = [];
	let depth = 0;
	let start = 0;
	for (let i = 0; i < input.length; i += 1) {
		const ch = input[i];
		if (ch === '(') depth += 1;
		else if (ch === ')') depth -= 1;
		else if (ch === ',' && depth === 0) {
			parts.push(input.slice(start, i).trim());
			start = i + 1;
		}
	}
	parts.push(input.slice(start).trim());
	return parts;
}

function resolveColorExpr(expr: string, vars: Record<string, string>, mode: Mode): Oklab {
	const trimmed = expr.replace(/\s+/g, ' ').trim();
	if (trimmed === 'white') return WHITE;
	if (trimmed === 'black') return BLACK;
	if (/^#[0-9a-f]{6}$/i.test(trimmed)) return parseHexToOklab(trimmed);

	if (trimmed.startsWith('var(') && trimmed.endsWith(')')) {
		const varName = trimmed.slice(4, -1).trim();
		if (varName === '--color-surface-50-950') {
			return resolveColorExpr(
				mode === 'light' ? 'var(--color-surface-50)' : 'var(--color-surface-950)',
				vars,
				mode,
			);
		}
		const target = vars[varName];
		if (!target) throw new Error(`Unknown CSS variable ${varName}`);
		return resolveColorExpr(target, vars, mode);
	}

	if (trimmed.startsWith('light-dark(') && trimmed.endsWith(')')) {
		const [lightExpr, darkExpr] = splitTopLevelArgs(trimmed.slice('light-dark('.length, -1));
		return resolveColorExpr(mode === 'light' ? lightExpr : darkExpr, vars, mode);
	}

	if (trimmed.startsWith('color-mix(') && trimmed.endsWith(')')) {
		const [space, firstArg, secondArg] = splitTopLevelArgs(trimmed.slice('color-mix('.length, -1));
		if (space.toLowerCase() !== 'in oklab') {
			throw new Error(`Unsupported color-mix space: ${space}`);
		}
		const parseWeighted = (arg: string): { color: Oklab; weight?: number } => {
			const match = arg.match(/^(.*)\s+([0-9.]+)%$/);
			if (!match) return { color: resolveColorExpr(arg, vars, mode) };
			return {
				color: resolveColorExpr(match[1].trim(), vars, mode),
				weight: parseFloat(match[2]) / 100,
			};
		};
		const first = parseWeighted(firstArg);
		const second = parseWeighted(secondArg);
		const w1 = first.weight ?? 1 - (second.weight ?? 0.5);
		const w2 = second.weight ?? 1 - w1;
		return {
			L: first.color.L * w1 + second.color.L * w2,
			a: first.color.a * w1 + second.color.a * w2,
			b: first.color.b * w1 + second.color.b * w2,
		};
	}

	const oklchMatch = trimmed.match(
		/^oklch\(\s*([0-9.]+|none)(%?)\s+([0-9.]+|none)\s+([0-9.]+|none)(?:deg)?(?:\s*\/\s*[0-9.%]+)?\s*\)$/i,
	);
	if (oklchMatch) {
		let L = oklchMatch[1] === 'none' ? 0 : parseFloat(oklchMatch[1]);
		if (oklchMatch[2] === '%') L /= 100;
		const C = oklchMatch[3] === 'none' ? 0 : parseFloat(oklchMatch[3]);
		const H = ((oklchMatch[4] === 'none' ? 0 : parseFloat(oklchMatch[4])) * Math.PI) / 180;
		return { L, a: C * Math.cos(H), b: C * Math.sin(H) };
	}

	throw new Error(`Unsupported color expression: ${trimmed}`);
}

function relativeLuminance({ L, a, b }: Oklab): number {
	const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
	const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
	const s_ = L - 0.0894841775 * a - 1.291485548 * b;
	const l = l_ * l_ * l_;
	const m = m_ * m_ * m_;
	const s = s_ * s_ * s_;
	const r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s;
	const g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s;
	const bl = -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s;
	const toSrgb = (c: number) => {
		const clamped = Math.max(0, Math.min(1, c));
		return clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * Math.pow(clamped, 1 / 2.4) - 0.055;
	};
	const toLin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
	return (
		0.2126 * toLin(toSrgb(r)) +
		0.7152 * toLin(toSrgb(g)) +
		0.0722 * toLin(toSrgb(bl))
	);
}

function contrastRatio(c1: Oklab, c2: Oklab): number {
	const lum1 = relativeLuminance(c1);
	const lum2 = relativeLuminance(c2);
	return (Math.max(lum1, lum2) + 0.05) / (Math.min(lum1, lum2) + 0.05);
}

function parseCssVars(css: string): Record<string, string> {
	const vars: Record<string, string> = {};
	for (const match of css.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) {
		vars[match[1]] = match[2].trim();
	}
	return vars;
}

const layoutCss = readFileSync(join(process.cwd(), 'src/routes/layout.css'), 'utf8');
const semanticVars = parseCssVars(layoutCss);

function loadThemeVars(theme: SkeletonThemeName): Record<string, string> {
	const themeCss = readFileSync(
		join(process.cwd(), `node_modules/@skeletonlabs/skeleton/src/themes/${theme}.css`),
		'utf8',
	);
	return { ...parseCssVars(themeCss), ...semanticVars };
}

const themeModePairs = THEMES.flatMap((theme) => [
	{ theme: theme.id, mode: 'light' as const },
	{ theme: theme.id, mode: 'dark' as const },
]);

describe('WCAG 2.2 non-text and focus contrast across all 48 theme-mode pairs', () => {
	it('defines semantic border, focus, switch, and star tokens in layout.css without bundling extra themes', () => {
		expect(themeModePairs).toHaveLength(48);
		for (const token of [
			'--control-border',
			'--focus-ring-inner',
			'--focus-ring-outer',
			'--switch-track-off',
			'--switch-thumb-off',
			'--switch-thumb-on',
			'--switch-track-primary',
			'--switch-track-secondary',
			'--switch-track-tertiary',
			'--star-idle-fg',
			'--star-idle-border',
			'--star-active-fg',
			'--star-active-border',
			'--star-active-bg',
		]) {
			expect(semanticVars[token], `missing ${token} in layout.css`).toBeTruthy();
		}
		expect(layoutCss).toContain('.input-ghost');
		expect(layoutCss).toContain('.input:has(.input-ghost:focus)');
	});

	it.each(themeModePairs)(
		'achieves >= 3:1 resting control borders across surfaces in $theme ($mode)',
		({ theme, mode }) => {
			const vars = loadThemeVars(theme);
			const border = resolveColorExpr('var(--control-border)', vars, mode);
			const surfaces =
				mode === 'light'
					? ['var(--color-root-bg-light)', 'var(--color-surface-50)', 'var(--color-surface-100)', 'var(--color-surface-200)']
					: ['var(--color-root-bg-dark)', 'var(--color-surface-950)', 'var(--color-surface-900)', 'var(--color-surface-800)'];

			for (const surfaceExpr of surfaces) {
				const surface = resolveColorExpr(surfaceExpr, vars, mode);
				expect(contrastRatio(border, surface)).toBeGreaterThanOrEqual(3);
			}
		},
	);

	it.each(themeModePairs)(
		'achieves >= 3:1 switch track boundaries, active/inactive states, and thumb contrast in $theme ($mode)',
		({ theme, mode }) => {
			const vars = loadThemeVars(theme);
			const border = resolveColorExpr('var(--control-border)', vars, mode);
			const trackOff = resolveColorExpr('var(--switch-track-off)', vars, mode);
			const thumbOff = resolveColorExpr('var(--switch-thumb-off)', vars, mode);
			const thumbOn = resolveColorExpr('var(--switch-thumb-on)', vars, mode);
			const surface = resolveColorExpr('var(--color-surface-50-950)', vars, mode);
			const rootBg = resolveColorExpr(
				mode === 'light' ? 'var(--color-root-bg-light)' : 'var(--color-root-bg-dark)',
				vars,
				mode,
			);

			expect(contrastRatio(border, surface)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(border, trackOff)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(thumbOff, trackOff)).toBeGreaterThanOrEqual(3);

			for (const role of ['primary', 'secondary', 'tertiary'] as const) {
				const trackOn = resolveColorExpr(`var(--switch-track-${role})`, vars, mode);
				expect(contrastRatio(trackOn, surface)).toBeGreaterThanOrEqual(3);
				expect(contrastRatio(trackOn, rootBg)).toBeGreaterThanOrEqual(3);
				expect(contrastRatio(trackOn, trackOff)).toBeGreaterThanOrEqual(3);
				expect(contrastRatio(thumbOn, trackOn)).toBeGreaterThanOrEqual(3);
			}
		},
	);

	it.each(themeModePairs)(
		'achieves >= 3:1 active and idle star glyphs and borders in $theme ($mode)',
		({ theme, mode }) => {
			const vars = loadThemeVars(theme);
			const dialogSurface = resolveColorExpr('var(--color-surface-50-950)', vars, mode);
			const idleFg = resolveColorExpr('var(--star-idle-fg)', vars, mode);
			const idleBorder = resolveColorExpr('var(--star-idle-border)', vars, mode);
			const idleBg = resolveColorExpr('var(--star-idle-bg)', vars, mode);
			const activeFg = resolveColorExpr('var(--star-active-fg)', vars, mode);
			const activeBorder = resolveColorExpr('var(--star-active-border)', vars, mode);
			const activeBg = resolveColorExpr('var(--star-active-bg)', vars, mode);

			expect(contrastRatio(idleFg, idleBg)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(idleBorder, dialogSurface)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(activeFg, activeBg)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(activeBorder, dialogSurface)).toBeGreaterThanOrEqual(3);
		},
	);

	it.each(themeModePairs)(
		'achieves >= 3:1 two-color focus contrast against surfaces and ThemePicker active swatches in $theme ($mode)',
		({ theme, mode }) => {
			const vars = loadThemeVars(theme);
			const focusInner = resolveColorExpr('var(--focus-ring-inner)', vars, mode);
			const focusOuter = resolveColorExpr('var(--focus-ring-outer)', vars, mode);
			const dialogSurface = resolveColorExpr('var(--color-surface-50-950)', vars, mode);
			const rootBg = resolveColorExpr(
				mode === 'light' ? 'var(--color-root-bg-light)' : 'var(--color-root-bg-dark)',
				vars,
				mode,
			);
			const activeSwatchFill = resolveColorExpr('var(--color-primary-500)', vars, mode);

			expect(contrastRatio(focusInner, focusOuter)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(focusOuter, dialogSurface)).toBeGreaterThanOrEqual(3);
			expect(contrastRatio(focusOuter, rootBg)).toBeGreaterThanOrEqual(3);
			expect(
				Math.max(
					contrastRatio(focusInner, activeSwatchFill),
					contrastRatio(focusOuter, activeSwatchFill),
				),
			).toBeGreaterThanOrEqual(3);
		},
	);
});
