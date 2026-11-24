/**
 * Server/build-time OKLCH color parsing, WCAG 2.2 Technique G18 relative luminance,
 * and contrast calibration for Skeleton theme stylesheets.
 *
 * Lives under `src/lib/server/` so SvelteKit guarantees calibration logic and
 * test/build helpers never enter the client JavaScript bundle.
 *
 * References:
 * - WCAG 2.2 SC 1.4.3 Contrast (Minimum): https://www.w3.org/TR/WCAG22/#contrast-minimum
 * - Technique G18 (4.5:1 normal text): https://www.w3.org/WAI/WCAG22/Techniques/general/G18
 * - Technique G145 (3:1 large text): https://www.w3.org/WAI/WCAG22/Techniques/general/G145
 * - WCAG 2.2 SC 1.4.11 Non-text Contrast: https://www.w3.org/TR/WCAG22/#non-text-contrast
 */

export const THEME_COLOR_ROLES = [
	'primary',
	'secondary',
	'tertiary',
	'success',
	'warning',
	'error',
	'surface',
] as const;

export type ThemeColorRole = (typeof THEME_COLOR_ROLES)[number];

export const THEME_SHADES = [
	'50',
	'100',
	'200',
	'300',
	'400',
	'500',
	'600',
	'700',
	'800',
	'900',
	'950',
] as const;

export type ThemeShade = (typeof THEME_SHADES)[number];

export type OklchColor = {
	L: number;
	C: number;
	H: number;
	hueNone?: boolean;
};

/** Default monochrome fallback tokens matching `@skeletonlabs/skeleton` base/theme.css. */
export const DEFAULT_SKELETON_ROOT_TOKENS: Readonly<Record<string, string>> = (() => {
	const baseScale: Record<ThemeShade, string> = {
		'50': 'oklch(0.985 0 0)',
		'100': 'oklch(0.97 0 0)',
		'200': 'oklch(0.922 0 0)',
		'300': 'oklch(0.87 0 0)',
		'400': 'oklch(0.708 0 0)',
		'500': 'oklch(0.556 0 0)',
		'600': 'oklch(0.439 0 0)',
		'700': 'oklch(0.371 0 0)',
		'800': 'oklch(0.269 0 0)',
		'900': 'oklch(0.205 0 0)',
		'950': 'oklch(0.145 0 0)',
	};
	const tokens: Record<string, string> = {
		'--color-root-bg-light': 'var(--color-surface-50)',
		'--color-root-bg-dark': 'var(--color-surface-950)',
		'--typo-base--color-light': 'var(--color-surface-950)',
		'--typo-base--color-dark': 'var(--color-surface-50)',
		'--typo-heading--color-light': 'inherit',
		'--typo-heading--color-dark': 'inherit',
		'--typo-anchor--color-light': 'var(--color-primary-500)',
		'--typo-anchor--color-dark': 'var(--color-primary-400)',
	};
	for (const role of THEME_COLOR_ROLES) {
		for (const shade of THEME_SHADES) {
			tokens[`--color-${role}-${shade}`] = baseScale[shade];
		}
		tokens[`--color-${role}-contrast-dark`] = `var(--color-${role}-950)`;
		tokens[`--color-${role}-contrast-light`] = `var(--color-${role}-50)`;
		for (const shade of ['50', '100', '200', '300'] as const) {
			tokens[`--color-${role}-contrast-${shade}`] = `var(--color-${role}-contrast-dark)`;
		}
		for (const shade of ['400', '500', '600', '700', '800', '900', '950'] as const) {
			tokens[`--color-${role}-contrast-${shade}`] = `var(--color-${role}-contrast-light)`;
		}
	}
	return tokens;
})();

const OKLCH_RE =
	/^oklch\(\s*([0-9.]+)(%?)\s+([0-9.]+)\s+(none|-?[0-9.]+)(?:deg)?(?:\s*\/\s*([0-9.]+%?))?\s*\)$/i;
const HEX_RE = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

function srgbChannelToLinear(channel: number): number {
	return channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4);
}

function linearChannelToSrgb(channel: number): number {
	const clamped = Math.min(1, Math.max(0, channel));
	return clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * Math.pow(clamped, 1 / 2.4) - 0.055;
}

function linearRgbToOklch(r: number, g: number, b: number): OklchColor {
	const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
	const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
	const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);

	const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
	const oa = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
	const ob = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;

	const C = Math.hypot(oa, ob);
	if (C < 0.0005) {
		return { L: Math.min(1, Math.max(0, L)), C: 0, H: 0, hueNone: true };
	}
	const hue = ((Math.atan2(ob, oa) * 180) / Math.PI + 360) % 360;
	return { L: Math.min(1, Math.max(0, L)), C, H: hue, hueNone: false };
}

/** Parse a CSS `oklch(...)` or `#hex` color string into OKLCH coordinates. */
export function parseCssColor(input: string): OklchColor {
	const value = input.trim();
	const oklchMatch = value.match(OKLCH_RE);
	if (oklchMatch) {
		const rawL = Number.parseFloat(oklchMatch[1]);
		const L = oklchMatch[2] === '%' ? rawL / 100 : rawL;
		const C = Number.parseFloat(oklchMatch[3]);
		const hueNone = oklchMatch[4].toLowerCase() === 'none' || C === 0;
		const rawH = hueNone ? 0 : Number.parseFloat(oklchMatch[4]);
		const H = ((rawH % 360) + 360) % 360;
		return { L: Math.min(1, Math.max(0, L)), C: Math.max(0, C), H, hueNone };
	}

	const hexMatch = value.match(HEX_RE);
	if (hexMatch) {
		const raw = hexMatch[1];
		const full =
			raw.length === 3 ? raw.split('').map((ch) => ch + ch).join('') : raw;
		const r = srgbChannelToLinear(Number.parseInt(full.slice(0, 2), 16) / 255);
		const g = srgbChannelToLinear(Number.parseInt(full.slice(2, 4), 16) / 255);
		const b = srgbChannelToLinear(Number.parseInt(full.slice(4, 6), 16) / 255);
		return linearRgbToOklch(r, g, b);
	}

	throw new Error(`Unsupported CSS color value: ${input}`);
}

export function oklchToLinearSrgb(color: OklchColor): [number, number, number] {
	const radians = (color.H * Math.PI) / 180;
	const a = color.C * Math.cos(radians);
	const b = color.C * Math.sin(radians);

	const lRoot = color.L + 0.3963377774 * a + 0.2158037573 * b;
	const mRoot = color.L - 0.1055613458 * a - 0.0638541728 * b;
	const sRoot = color.L - 0.0894841775 * a - 1.291485548 * b;

	const l = lRoot * lRoot * lRoot;
	const m = mRoot * mRoot * mRoot;
	const s = sRoot * sRoot * sRoot;

	return [
		4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
		-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
		-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
	];
}

/** Convert an `OklchColor` or CSS color string to 8-bit sRGB `#rrggbb` hex. */
export function oklchToHex(input: string | OklchColor): string {
	const color = typeof input === 'string' ? parseCssColor(input) : input;
	const linear = oklchToLinearSrgb(color);
	return `#${linear
		.map((channel) => Math.round(linearChannelToSrgb(channel) * 255).toString(16).padStart(2, '0'))
		.join('')}`;
}

function isInSrgbGamut(color: OklchColor): boolean {
	const [r, g, b] = oklchToLinearSrgb(color);
	const eps = 1e-6;
	return r >= -eps && r <= 1 + eps && g >= -eps && g <= 1 + eps && b >= -eps && b <= 1 + eps;
}

/**
 * Clamp chroma into the sRGB gamut at the given lightness and hue so browsers
 * that clip channels and browsers that gamut-map OKLCH render identical pixels.
 */
export function clampOklchToSrgbGamut(color: OklchColor): OklchColor {
	const L = Math.min(1, Math.max(0, color.L));
	if (L <= 0.0001 || L >= 0.9999 || color.C <= 0.0005 || color.hueNone) {
		return { L, C: 0, H: color.hueNone ? 0 : color.H, hueNone: true };
	}
	const candidate: OklchColor = { L, C: color.C, H: color.H, hueNone: false };
	if (isInSrgbGamut(candidate)) return candidate;

	let lo = 0;
	let hi = color.C;
	for (let step = 0; step < 24; step += 1) {
		const mid = (lo + hi) / 2;
		if (isInSrgbGamut({ L, C: mid, H: color.H, hueNone: false })) lo = mid;
		else hi = mid;
	}
	return { L, C: lo, H: color.H, hueNone: lo < 0.0005 };
}

function round4(value: number): string {
	const rounded = Math.round(value * 10000) / 10000;
	return Object.is(rounded, -0) ? '0' : String(rounded);
}

function round2(value: number): string {
	const rounded = Math.round(value * 100) / 100;
	return Object.is(rounded, -0) ? '0' : String(rounded);
}

/** Format an OKLCH color as standard `oklch(L C H)` supported by Tailwind v4 browsers. */
export function formatOklch(color: OklchColor): string {
	const clamped = clampOklchToSrgbGamut(color);
	if (clamped.C < 0.0005 || clamped.hueNone) {
		return `oklch(${round4(clamped.L)} 0 0)`;
	}
	return `oklch(${round4(clamped.L)} ${round4(clamped.C)} ${round2(clamped.H)})`;
}

/** WCAG 2.2 Technique G18 relative luminance in continuous sRGB. */
export function relativeLuminanceContinuous(color: OklchColor): number {
	const [rLin, gLin, bLin] = oklchToLinearSrgb(color);
	const r = srgbChannelToLinear(linearChannelToSrgb(rLin));
	const g = srgbChannelToLinear(linearChannelToSrgb(gLin));
	const b = srgbChannelToLinear(linearChannelToSrgb(bLin));
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.2 Technique G18 relative luminance after 8-bit sRGB quantization (matching DOM/canvas). */
export function relativeLuminance8Bit(color: OklchColor): number {
	const [rLin, gLin, bLin] = oklchToLinearSrgb(color);
	const r8 = Math.round(linearChannelToSrgb(rLin) * 255) / 255;
	const g8 = Math.round(linearChannelToSrgb(gLin) * 255) / 255;
	const b8 = Math.round(linearChannelToSrgb(bLin) * 255) / 255;
	return (
		0.2126 * srgbChannelToLinear(r8) +
		0.7152 * srgbChannelToLinear(g8) +
		0.0722 * srgbChannelToLinear(b8)
	);
}

function g18Ratio(lum1: number, lum2: number): number {
	const lighter = Math.max(lum1, lum2);
	const darker = Math.min(lum1, lum2);
	return (lighter + 0.05) / (darker + 0.05);
}

/**
 * Conservative Technique G18 contrast ratio taking the minimum of continuous sRGB
 * and 8-bit quantized sRGB so both mathematical checks and browser `axe-core` pass.
 */
export function contrastRatio(foreground: OklchColor, background: OklchColor): number {
	const continuous = g18Ratio(
		relativeLuminanceContinuous(foreground),
		relativeLuminanceContinuous(background),
	);
	const quantized = g18Ratio(
		relativeLuminance8Bit(foreground),
		relativeLuminance8Bit(background),
	);
	return Math.min(continuous, quantized);
}

function minContrastAgainstAll(foreground: OklchColor, backgrounds: readonly OklchColor[]): number {
	let min = Number.POSITIVE_INFINITY;
	for (const bg of backgrounds) {
		const ratio = contrastRatio(foreground, bg);
		if (ratio < min) min = ratio;
	}
	return min;
}

/** Mix `overlay` onto `base` in Oklab space (`color-mix(in oklab, overlay <weight>, base)`). */
export function mixOklab(base: OklchColor, overlay: OklchColor, weight: number): OklchColor {
	const baseRad = (base.H * Math.PI) / 180;
	const overRad = (overlay.H * Math.PI) / 180;
	const L = base.L * (1 - weight) + overlay.L * weight;
	const a =
		base.C * Math.cos(baseRad) * (1 - weight) + overlay.C * Math.cos(overRad) * weight;
	const b =
		base.C * Math.sin(baseRad) * (1 - weight) + overlay.C * Math.sin(overRad) * weight;
	const C = Math.hypot(a, b);
	if (C < 0.0005) return clampOklchToSrgbGamut({ L, C: 0, H: 0, hueNone: true });
	const H = ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360;
	return clampOklchToSrgbGamut({ L, C, H, hueNone: false });
}

/** Composite `overlay` at `alpha` over `base` in standard sRGB alpha blending (`bg-*-500/<alpha>`). */
export function mixSrgbAlpha(base: OklchColor, overlay: OklchColor, alpha: number): OklchColor {
	const [br, bg, bb] = oklchToLinearSrgb(base);
	const [or, og, ob] = oklchToLinearSrgb(overlay);
	const rSrgb = linearChannelToSrgb(or) * alpha + linearChannelToSrgb(br) * (1 - alpha);
	const gSrgb = linearChannelToSrgb(og) * alpha + linearChannelToSrgb(bg) * (1 - alpha);
	const bSrgb = linearChannelToSrgb(ob) * alpha + linearChannelToSrgb(bb) * (1 - alpha);
	return clampOklchToSrgbGamut(
		linearRgbToOklch(
			srgbChannelToLinear(rSrgb),
			srgbChannelToLinear(gSrgb),
			srgbChannelToLinear(bSrgb),
		),
	);
}

/**
 * Shift `color.L` minimally in `direction` until `minContrastAgainstAll(candidate, backgrounds) >= minRatio`,
 * preserving hue angle and clamping chroma to the sRGB gamut.
 */
export function calibrateLightnessForContrast(
	color: OklchColor,
	backgrounds: readonly OklchColor[],
	minRatio: number,
	direction: 'darker' | 'lighter',
): OklchColor {
	const initial = clampOklchToSrgbGamut(color);
	if (minContrastAgainstAll(initial, backgrounds) >= minRatio) {
		const formattedInitial = parseCssColor(formatOklch(initial));
		if (minContrastAgainstAll(formattedInitial, backgrounds) >= minRatio) {
			return formattedInitial;
		}
	}

	let lo = direction === 'darker' ? 0 : initial.L;
	let hi = direction === 'darker' ? initial.L : 1;
	let best = parseCssColor(
		formatOklch(clampOklchToSrgbGamut({ ...initial, L: direction === 'darker' ? 0 : 1 })),
	);

	for (let step = 0; step < 28; step += 1) {
		const mid = (lo + hi) / 2;
		const candidate = parseCssColor(formatOklch(clampOklchToSrgbGamut({ ...initial, L: mid })));
		if (minContrastAgainstAll(candidate, backgrounds) >= minRatio) {
			best = candidate;
			if (direction === 'darker') lo = mid;
			else hi = mid;
		} else if (direction === 'darker') {
			hi = mid;
		} else {
			lo = mid;
		}
	}
	return best;
}

/** Extract `--custom-property: value;` declarations from a theme stylesheet. */
export function extractCssVariables(stylesheet: string): Record<string, string> {
	const vars: Record<string, string> = {};
	const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;{}]+);/g;
	for (const match of stylesheet.matchAll(declRe)) {
		vars[match[1].trim()] = match[2].trim();
	}
	return vars;
}

function resolveTokenValue(
	name: string,
	rawVars: Readonly<Record<string, string>>,
	fallbackVars: Readonly<Record<string, string>> = DEFAULT_SKELETON_ROOT_TOKENS,
): string {
	let current = rawVars[name] ?? fallbackVars[name];
	let depth = 0;
	while (current && current.startsWith('var(') && depth < 12) {
		const refMatch = current.match(/^var\(\s*(--[a-zA-Z0-9-]+)(?:\s*,\s*([^)]+))?\s*\)$/);
		if (!refMatch) break;
		const refName = refMatch[1];
		const inlineFallback = refMatch[2]?.trim();
		current = rawVars[refName] ?? fallbackVars[refName] ?? inlineFallback ?? '';
		depth += 1;
	}
	return current ?? '';
}

export type CalibratedModeTokens = Record<string, string>;

export type CalibratedThemeResult = {
	/**
	 * Base tokens emitted on `:root[data-theme="<id>"]` (applies to light mode and
	 * provides shared scale tokens such as `--color-surface-contrast-50` / `-950`
	 * inherited by dark mode unless overridden in `dark`).
	 */
	light: CalibratedModeTokens;
	/** Mode-specific overrides emitted on `:root[data-theme="<id>"].dark`. */
	dark: CalibratedModeTokens;
};

const CORE_TEXT_TARGET_RATIO = 4.9;
const EXTENDED_MIX_TEXT_TARGET_RATIO = 4.65;
const FILLED_TARGET_RATIO = 4.7;
const TONAL_TARGET_RATIO = 5.0;
const TONAL_MIX_TARGET_RATIO = 4.65;
const SWITCH_TARGET_RATIO = 3.1;

function calibrateAgainstCoreAndExtended(
	initial: OklchColor,
	coreBackgrounds: readonly OklchColor[],
	extendedBackgrounds: readonly OklchColor[],
	coreRatio: number,
	extendedRatio: number,
	direction: 'darker' | 'lighter',
): OklchColor {
	const coreAdjusted = calibrateLightnessForContrast(
		initial,
		coreBackgrounds,
		coreRatio,
		direction,
	);
	return calibrateLightnessForContrast(
		coreAdjusted,
		extendedBackgrounds,
		extendedRatio,
		direction,
	);
}

function calibrateFilledPairForMode(
	orig500: OklchColor,
	origContrast500: OklchColor,
	role50: OklchColor,
	role950: OklchColor,
	surfaceBg: OklchColor,
	mode: 'light' | 'dark',
	requireSwitchBoundary: boolean,
): { c500: OklchColor; contrast500: OklchColor } {
	let c500 = parseCssColor(formatOklch(orig500));
	if (requireSwitchBoundary && contrastRatio(c500, surfaceBg) < SWITCH_TARGET_RATIO) {
		c500 = calibrateLightnessForContrast(
			c500,
			[surfaceBg],
			SWITCH_TARGET_RATIO,
			mode === 'light' ? 'darker' : 'lighter',
		);
	}

	const preferDarkText =
		relativeLuminanceContinuous(origContrast500) < relativeLuminanceContinuous(orig500);
	const lightEndpoint = parseCssColor(
		formatOklch({
			L: Math.max(0.985, role50.L),
			C: Math.min(orig500.C, 0.015),
			H: orig500.H,
			hueNone: orig500.hueNone,
		}),
	);
	const darkEndpoint = parseCssColor(
		formatOklch({
			L: Math.min(0.12, role950.L),
			C: Math.min(orig500.C, 0.03),
			H: orig500.H,
			hueNone: orig500.hueNone,
		}),
	);

	const initialContrast = parseCssColor(formatOklch(origContrast500));
	if (contrastRatio(c500, initialContrast) >= FILLED_TARGET_RATIO) {
		return { c500, contrast500: initialContrast };
	}

	const primaryCandidate = preferDarkText ? darkEndpoint : lightEndpoint;
	const secondaryCandidate = preferDarkText ? lightEndpoint : darkEndpoint;

	if (contrastRatio(c500, primaryCandidate) >= FILLED_TARGET_RATIO) {
		const tuned = calibrateLightnessForContrast(
			initialContrast,
			[c500],
			FILLED_TARGET_RATIO,
			preferDarkText ? 'darker' : 'lighter',
		);
		return {
			c500,
			contrast500: contrastRatio(c500, tuned) >= FILLED_TARGET_RATIO ? tuned : primaryCandidate,
		};
	}

	if (contrastRatio(c500, secondaryCandidate) >= FILLED_TARGET_RATIO) {
		const altTuned = calibrateLightnessForContrast(
			preferDarkText ? role50 : role950,
			[c500],
			FILLED_TARGET_RATIO,
			preferDarkText ? 'lighter' : 'darker',
		);
		return {
			c500,
			contrast500:
				contrastRatio(c500, altTuned) >= FILLED_TARGET_RATIO ? altTuned : secondaryCandidate,
		};
	}

	if (mode === 'light') {
		const adjusted500 = calibrateLightnessForContrast(
			c500,
			[lightEndpoint],
			FILLED_TARGET_RATIO,
			'darker',
		);
		return { c500: adjusted500, contrast500: lightEndpoint };
	}
	const adjusted500 = calibrateLightnessForContrast(
		c500,
		[darkEndpoint],
		FILLED_TARGET_RATIO,
		'lighter',
	);
	return { c500: adjusted500, contrast500: darkEndpoint };
}

function buildTranslucentBackgroundEnvelope(
	surfaceBase: OklchColor,
	rootBase: OklchColor,
	oppositeSurface: OklchColor,
	primary500: OklchColor,
	secondary500: OklchColor,
	tertiary500: OklchColor,
): { core: OklchColor[]; extended: OklchColor[] } {
	const core: OklchColor[] = [
		surfaceBase,
		rootBase,
		// 10% opposite surface (`preset-tonal` in Oklab + `bg-surface-950-50/10` in sRGB)
		mixOklab(surfaceBase, oppositeSurface, 0.1),
		mixSrgbAlpha(surfaceBase, oppositeSurface, 0.1),
		mixSrgbAlpha(surfaceBase, primary500, 0.08),
		mixSrgbAlpha(surfaceBase, secondary500, 0.08),
	];

	const extended: OklchColor[] = [
		...core,
		// Modal and callout 15%-20% role-500 tints (`bg-primary-500/15`, `bg-secondary-500/10`, `bg-primary-500/20`)
		mixSrgbAlpha(surfaceBase, primary500, 0.15),
		mixSrgbAlpha(surfaceBase, secondary500, 0.15),
		mixSrgbAlpha(surfaceBase, tertiary500, 0.15),
		mixSrgbAlpha(surfaceBase, primary500, 0.2),
		mixSrgbAlpha(surfaceBase, secondary500, 0.2),
		mixSrgbAlpha(surfaceBase, tertiary500, 0.2),
		mixOklab(surfaceBase, primary500, 0.2),
		mixOklab(surfaceBase, secondary500, 0.2),
		mixOklab(surfaceBase, tertiary500, 0.2),
		mixSrgbAlpha(rootBase, primary500, 0.2),
		// Stacked book card + mobile cover overlay (`from-primary-500/8` + `to-secondary-500/15`, etc.)
		mixSrgbAlpha(mixSrgbAlpha(surfaceBase, primary500, 0.08), secondary500, 0.15),
		mixSrgbAlpha(mixSrgbAlpha(surfaceBase, secondary500, 0.08), primary500, 0.15),
		// Login page ambient radial glows (`30%` primary-500, `26%` secondary-500, `23%` tertiary-500)
		mixOklab(surfaceBase, primary500, 0.3),
		mixSrgbAlpha(surfaceBase, primary500, 0.3),
		mixOklab(surfaceBase, secondary500, 0.26),
		mixSrgbAlpha(surfaceBase, secondary500, 0.26),
		mixOklab(surfaceBase, tertiary500, 0.23),
		mixSrgbAlpha(surfaceBase, tertiary500, 0.23),
	];

	return { core, extended };
}

/**
 * Compute calibrated light and dark OKLCH token overrides for a Skeleton theme variable map.
 */
export function calibrateThemeTokens(
	rawVars: Readonly<Record<string, string>>,
): CalibratedThemeResult {
	const colorOf = (name: string): OklchColor =>
		parseCssColor(resolveTokenValue(name, rawVars));

	// 1. Normalize surface scale endpoints and monotonicity (fixes reign, sahara, fennec, mint, etc.).
	const rawSurfaceScale = THEME_SHADES.map((shade) => colorOf(`--color-surface-${shade}`));
	let surface50 = rawSurfaceScale[0];
	if (surface50.L < 0.955) {
		surface50 = clampOklchToSrgbGamut({
			L: 0.965,
			C: Math.min(surface50.C, 0.025),
			H: surface50.H,
			hueNone: surface50.hueNone,
		});
	} else {
		surface50 = clampOklchToSrgbGamut(surface50);
	}

	let surface950 = rawSurfaceScale[10];
	if (surface950.L > 0.22) {
		surface950 = clampOklchToSrgbGamut({
			L: 0.195,
			C: Math.min(surface950.C, 0.045),
			H: surface950.H,
			hueNone: surface950.hueNone,
		});
	} else {
		surface950 = clampOklchToSrgbGamut(surface950);
	}

	surface50 = parseCssColor(formatOklch(surface50));
	surface950 = parseCssColor(formatOklch(surface950));

	// Normalize root backgrounds so pages using `html` background and `<meta name="theme-color">`
	// remain genuinely light/dark and deterministic across themes.
	const rawRootLightRef = rawVars['--color-root-bg-light'] ?? 'var(--color-surface-50)';
	const rawRootDarkRef = rawVars['--color-root-bg-dark'] ?? 'var(--color-surface-950)';
	let rootBgLight =
		rawRootLightRef === 'var(--color-surface-50)' ? surface50 : colorOf('--color-root-bg-light');
	if (rootBgLight.L < 0.945) {
		rootBgLight = clampOklchToSrgbGamut({
			L: 0.955,
			C: Math.min(rootBgLight.C, 0.025),
			H: rootBgLight.H,
			hueNone: rootBgLight.hueNone,
		});
	}
	let rootBgDark =
		rawRootDarkRef === 'var(--color-surface-950)' ? surface950 : colorOf('--color-root-bg-dark');
	if (rootBgDark.L > 0.235) {
		rootBgDark = clampOklchToSrgbGamut({
			L: 0.205,
			C: Math.min(rootBgDark.C, 0.045),
			H: rootBgDark.H,
			hueNone: rootBgDark.hueNone,
		});
	}
	rootBgLight = parseCssColor(formatOklch(rootBgLight));
	rootBgDark = parseCssColor(formatOklch(rootBgDark));

	// Build a strictly monotonic initial 11-step surface scale.
	let surfaceInverted = false;
	for (let i = 0; i < rawSurfaceScale.length - 1; i += 1) {
		if (rawSurfaceScale[i].L <= rawSurfaceScale[i + 1].L) {
			surfaceInverted = true;
			break;
		}
	}
	const calibratedSurface: Record<ThemeShade, OklchColor> = {
		'50': surface50,
		'100': rawSurfaceScale[1],
		'200': rawSurfaceScale[2],
		'300': rawSurfaceScale[3],
		'400': rawSurfaceScale[4],
		'500': rawSurfaceScale[5],
		'600': rawSurfaceScale[6],
		'700': rawSurfaceScale[7],
		'800': rawSurfaceScale[8],
		'900': rawSurfaceScale[9],
		'950': surface950,
	};

	if (surfaceInverted || rawSurfaceScale[0].L < 0.88 || rawSurfaceScale[10].L > 0.3) {
		const targetWeights: Record<ThemeShade, number> = {
			'50': 0,
			'100': 0.08,
			'200': 0.18,
			'300': 0.3,
			'400': 0.42,
			'500': 0.54,
			'600': 0.65,
			'700': 0.75,
			'800': 0.84,
			'900': 0.92,
			'950': 1,
		};
		for (const shade of THEME_SHADES) {
			if (shade === '50' || shade === '950') continue;
			const orig = rawSurfaceScale[THEME_SHADES.indexOf(shade)];
			const w = targetWeights[shade];
			const interpL = surface50.L * (1 - w) + surface950.L * w;
			calibratedSurface[shade] = parseCssColor(
				formatOklch({
					L: interpL,
					C: Math.min(orig.C, 0.08),
					H: orig.H,
					hueNone: orig.hueNone,
				}),
			);
		}
	}

	// 2. Pre-calibrate tonal endpoints and filled 500 pairs for all roles so translucent
	// card/modal/login background envelopes use each mode's actual emitted `--color-*-500`.
	const switchRoles = new Set<ThemeColorRole>(['primary', 'secondary', 'tertiary', 'surface']);
	const roleTonalEndpoints = {} as Record<ThemeColorRole, { c50: OklchColor; c950: OklchColor }>;
	const roleFilled = {} as Record<
		ThemeColorRole,
		{
			light: { c500: OklchColor; contrast500: OklchColor };
			dark: { c500: OklchColor; contrast500: OklchColor };
		}
	>;

	for (const role of THEME_COLOR_ROLES) {
		let c50 = role === 'surface' ? surface50 : colorOf(`--color-${role}-50`);
		let c950 = role === 'surface' ? surface950 : colorOf(`--color-${role}-950`);

		if (role !== 'surface') {
			if (c50.L < 0.935) {
				c50 = clampOklchToSrgbGamut({
					L: 0.945,
					C: Math.min(c50.C, 0.045),
					H: c50.H,
					hueNone: c50.hueNone,
				});
			}
			if (c950.L > 0.3) {
				c950 = clampOklchToSrgbGamut({
					L: 0.26,
					C: Math.min(c950.C, 0.085),
					H: c950.H,
					hueNone: c950.hueNone,
				});
			}
			c50 = parseCssColor(formatOklch(c50));
			c950 = calibrateLightnessForContrast(c950, [c50], TONAL_TARGET_RATIO, 'darker');
			c50 = calibrateLightnessForContrast(c50, [c950], TONAL_TARGET_RATIO, 'lighter');
		}
		roleTonalEndpoints[role] = { c50, c950 };

		const orig500 = role === 'surface' ? calibratedSurface['500'] : colorOf(`--color-${role}-500`);
		const origContrast500 = colorOf(`--color-${role}-contrast-500`);
		const needsSwitchContrast = switchRoles.has(role);

		roleFilled[role] = {
			light: calibrateFilledPairForMode(
				orig500,
				origContrast500,
				c50,
				c950,
				surface50,
				'light',
				needsSwitchContrast,
			),
			dark: calibrateFilledPairForMode(
				orig500,
				origContrast500,
				c50,
				c950,
				surface950,
				'dark',
				needsSwitchContrast,
			),
		};
	}

	// 3. Build core + extended translucent background envelopes (10%-30%) using calibrated 500s.
	const lightEnvelope = buildTranslucentBackgroundEnvelope(
		surface50,
		rootBgLight,
		surface950,
		roleFilled.primary.light.c500,
		roleFilled.secondary.light.c500,
		roleFilled.tertiary.light.c500,
	);
	const darkEnvelope = buildTranslucentBackgroundEnvelope(
		surface950,
		rootBgDark,
		surface50,
		roleFilled.primary.dark.c500,
		roleFilled.secondary.dark.c500,
		roleFilled.tertiary.dark.c500,
	);

	// 4. Calibrate light-mode surface text shades (600, 700, 800, 900, 950) with strict monotonicity.
	calibratedSurface['600'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['600'],
			L: Math.min(calibratedSurface['600'].L, roleFilled.surface.light.c500.L - 0.02),
		},
		lightEnvelope.core,
		lightEnvelope.extended,
		CORE_TEXT_TARGET_RATIO,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	calibratedSurface['700'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['700'],
			L: Math.min(calibratedSurface['700'].L, calibratedSurface['600'].L - 0.025),
		},
		lightEnvelope.core,
		lightEnvelope.extended,
		5.6,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	calibratedSurface['800'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['800'],
			L: Math.min(calibratedSurface['800'].L, calibratedSurface['700'].L - 0.03),
		},
		lightEnvelope.core,
		lightEnvelope.extended,
		7.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	calibratedSurface['900'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['900'],
			L: Math.min(calibratedSurface['900'].L, calibratedSurface['800'].L - 0.03),
		},
		lightEnvelope.core,
		lightEnvelope.extended,
		8.5,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	calibratedSurface['950'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['950'],
			L: Math.min(calibratedSurface['950'].L, calibratedSurface['900'].L - 0.02),
		},
		lightEnvelope.core,
		lightEnvelope.extended,
		10.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	surface950 = calibratedSurface['950'];

	// Calibrate dark-mode surface text shades (400, 300, 200, 100, 50) with strict monotonicity.
	const maxSurface500L = Math.max(
		roleFilled.surface.light.c500.L,
		roleFilled.surface.dark.c500.L,
	);
	calibratedSurface['400'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['400'],
			L: Math.max(calibratedSurface['400'].L, maxSurface500L + 0.02),
		},
		darkEnvelope.core,
		darkEnvelope.extended,
		CORE_TEXT_TARGET_RATIO,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	calibratedSurface['300'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['300'],
			L: Math.max(calibratedSurface['300'].L, calibratedSurface['400'].L + 0.03),
		},
		darkEnvelope.core,
		darkEnvelope.extended,
		5.8,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	calibratedSurface['200'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['200'],
			L: Math.max(calibratedSurface['200'].L, calibratedSurface['300'].L + 0.04),
		},
		darkEnvelope.core,
		darkEnvelope.extended,
		7.5,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	calibratedSurface['100'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['100'],
			L: Math.max(calibratedSurface['100'].L, calibratedSurface['200'].L + 0.04),
		},
		darkEnvelope.core,
		darkEnvelope.extended,
		9.5,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	calibratedSurface['50'] = calibrateAgainstCoreAndExtended(
		{
			...calibratedSurface['50'],
			L: Math.max(calibratedSurface['50'].L, calibratedSurface['100'].L + 0.02),
		},
		darkEnvelope.core,
		darkEnvelope.extended,
		11.5,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	surface50 = calibratedSurface['50'];
	roleTonalEndpoints.surface = { c50: surface50, c950: surface950 };

	// Rebuild envelopes with final `surface50` / `surface950` so all remaining shades and
	// contrast pairs are guaranteed against the exact emitted surface endpoints.
	const finalLightEnvelope = buildTranslucentBackgroundEnvelope(
		surface50,
		rootBgLight,
		surface950,
		roleFilled.primary.light.c500,
		roleFilled.secondary.light.c500,
		roleFilled.tertiary.light.c500,
	);
	const finalDarkEnvelope = buildTranslucentBackgroundEnvelope(
		surface950,
		rootBgDark,
		surface50,
		roleFilled.primary.dark.c500,
		roleFilled.secondary.dark.c500,
		roleFilled.tertiary.dark.c500,
	);

	// `--color-surface-contrast-50` is dark foreground text paired with `--color-surface-50`
	// across both modes (including `preset-filled-surface-950-50` in dark mode), while
	// `--color-surface-contrast-950` is light foreground text paired with `--color-surface-950`.
	const surfaceContrast50 = calibrateAgainstCoreAndExtended(
		colorOf('--color-surface-contrast-50'),
		finalLightEnvelope.core,
		finalLightEnvelope.extended,
		9.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	const surfaceContrast950 = calibrateAgainstCoreAndExtended(
		colorOf('--color-surface-contrast-950'),
		finalDarkEnvelope.core,
		finalDarkEnvelope.extended,
		9.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);

	const light: CalibratedModeTokens = {
		'--color-root-bg-light': formatOklch(rootBgLight),
		'--color-root-bg-dark': formatOklch(rootBgDark),
	};
	const dark: CalibratedModeTokens = {};

	for (const shade of THEME_SHADES) {
		if (shade === '500') continue;
		light[`--color-surface-${shade}`] = formatOklch(calibratedSurface[shade]);
	}
	light['--color-surface-contrast-50'] = formatOklch(surfaceContrast50);
	light['--color-surface-contrast-950'] = formatOklch(surfaceContrast950);
	light['--color-surface-contrast-50-950'] = formatOklch(surfaceContrast50);
	dark['--color-surface-contrast-50-950'] = formatOklch(surfaceContrast950);
	light['--color-surface-contrast-950-50'] = formatOklch(surfaceContrast950);
	dark['--color-surface-contrast-950-50'] = formatOklch(surfaceContrast50);

	light['--color-surface-700-300'] = formatOklch(calibratedSurface['700']);
	dark['--color-surface-700-300'] = formatOklch(calibratedSurface['300']);
	light['--color-surface-800-200'] = formatOklch(calibratedSurface['800']);
	dark['--color-surface-800-200'] = formatOklch(calibratedSurface['200']);
	light['--color-surface-900-100'] = formatOklch(calibratedSurface['900']);
	dark['--color-surface-900-100'] = formatOklch(calibratedSurface['100']);

	// 5. Finalize all 7 roles: tonal (50/950 + card/code overlays), link/muted (600/400), and filled (500/contrast-500).
	for (const role of THEME_COLOR_ROLES) {
		let { c50, c950 } = roleTonalEndpoints[role];

		if (role !== 'surface') {
			const lightTonalMixes = [
				c50,
				mixSrgbAlpha(c50, roleFilled.primary.light.c500, 0.15),
				mixSrgbAlpha(c50, roleFilled.secondary.light.c500, 0.15),
				mixSrgbAlpha(c50, surface950, 0.1),
			];
			c950 = calibrateAgainstCoreAndExtended(
				c950,
				[c50],
				lightTonalMixes,
				TONAL_TARGET_RATIO,
				TONAL_MIX_TARGET_RATIO,
				'darker',
			);

			const darkTonalMixes = [
				c950,
				mixSrgbAlpha(c950, roleFilled.primary.dark.c500, 0.15),
				mixSrgbAlpha(c950, roleFilled.secondary.dark.c500, 0.15),
				mixSrgbAlpha(c950, surface50, 0.1),
			];
			c50 = calibrateAgainstCoreAndExtended(
				c50,
				[c950],
				darkTonalMixes,
				TONAL_TARGET_RATIO,
				TONAL_MIX_TARGET_RATIO,
				'lighter',
			);

			// Re-verify light tonal fg if c50 shifted slightly lighter.
			const updatedLightTonalMixes = [
				c50,
				mixSrgbAlpha(c50, roleFilled.primary.light.c500, 0.15),
				mixSrgbAlpha(c50, roleFilled.secondary.light.c500, 0.15),
				mixSrgbAlpha(c50, surface950, 0.1),
			];
			c950 = calibrateAgainstCoreAndExtended(
				c950,
				[c50],
				updatedLightTonalMixes,
				TONAL_TARGET_RATIO,
				TONAL_MIX_TARGET_RATIO,
				'darker',
			);

			light[`--color-${role}-50`] = formatOklch(c50);
			light[`--color-${role}-950`] = formatOklch(c950);
		}

		light[`--color-${role}-50-950`] = formatOklch(c50);
		light[`--color-${role}-950-50`] = formatOklch(c950);
		dark[`--color-${role}-50-950`] = formatOklch(c950);
		dark[`--color-${role}-950-50`] = formatOklch(c50);

		const c600 =
			role === 'surface'
				? calibratedSurface['600']
				: calibrateAgainstCoreAndExtended(
						colorOf(`--color-${role}-600`),
						finalLightEnvelope.core,
						finalLightEnvelope.extended,
						CORE_TEXT_TARGET_RATIO,
						EXTENDED_MIX_TEXT_TARGET_RATIO,
						'darker',
					);
		const c400 =
			role === 'surface'
				? calibratedSurface['400']
				: calibrateAgainstCoreAndExtended(
						colorOf(`--color-${role}-400`),
						finalDarkEnvelope.core,
						finalDarkEnvelope.extended,
						CORE_TEXT_TARGET_RATIO,
						EXTENDED_MIX_TEXT_TARGET_RATIO,
						'lighter',
					);

		if (role !== 'surface') {
			light[`--color-${role}-600`] = formatOklch(c600);
			light[`--color-${role}-400`] = formatOklch(c400);
		}
		light[`--color-${role}-600-400`] = formatOklch(c600);
		dark[`--color-${role}-600-400`] = formatOklch(c400);

		const lightFilled = roleFilled[role].light;
		const darkFilled = roleFilled[role].dark;

		light[`--color-${role}-500`] = formatOklch(lightFilled.c500);
		light[`--color-${role}-contrast-500`] = formatOklch(lightFilled.contrast500);

		const dark500Str = formatOklch(darkFilled.c500);
		const darkContrastStr = formatOklch(darkFilled.contrast500);
		if (dark500Str !== light[`--color-${role}-500`]) {
			dark[`--color-${role}-500`] = dark500Str;
		}
		if (darkContrastStr !== light[`--color-${role}-contrast-500`]) {
			dark[`--color-${role}-contrast-500`] = darkContrastStr;
		}
	}

	// 6. Calibrate base, heading, and anchor typography tokens against core + extended envelopes.
	const typoBaseLight = calibrateAgainstCoreAndExtended(
		colorOf('--typo-base--color-light'),
		finalLightEnvelope.core,
		finalLightEnvelope.extended,
		9.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	const typoBaseDark = calibrateAgainstCoreAndExtended(
		colorOf('--typo-base--color-dark'),
		finalDarkEnvelope.core,
		finalDarkEnvelope.extended,
		9.0,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	light['--typo-base--color-light'] = formatOklch(typoBaseLight);
	light['--typo-base--color-dark'] = formatOklch(typoBaseDark);

	const rawHeadingLight = resolveTokenValue('--typo-heading--color-light', rawVars);
	if (rawHeadingLight && rawHeadingLight !== 'inherit') {
		light['--typo-heading--color-light'] = formatOklch(
			calibrateAgainstCoreAndExtended(
				parseCssColor(rawHeadingLight),
				finalLightEnvelope.core,
				finalLightEnvelope.extended,
				CORE_TEXT_TARGET_RATIO,
				EXTENDED_MIX_TEXT_TARGET_RATIO,
				'darker',
			),
		);
	}
	const rawHeadingDark = resolveTokenValue('--typo-heading--color-dark', rawVars);
	if (rawHeadingDark && rawHeadingDark !== 'inherit') {
		light['--typo-heading--color-dark'] = formatOklch(
			calibrateAgainstCoreAndExtended(
				parseCssColor(rawHeadingDark),
				finalDarkEnvelope.core,
				finalDarkEnvelope.extended,
				CORE_TEXT_TARGET_RATIO,
				EXTENDED_MIX_TEXT_TARGET_RATIO,
				'lighter',
			),
		);
	}

	const typoAnchorLight = calibrateAgainstCoreAndExtended(
		colorOf('--typo-anchor--color-light'),
		finalLightEnvelope.core,
		finalLightEnvelope.extended,
		CORE_TEXT_TARGET_RATIO,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'darker',
	);
	const typoAnchorDark = calibrateAgainstCoreAndExtended(
		colorOf('--typo-anchor--color-dark'),
		finalDarkEnvelope.core,
		finalDarkEnvelope.extended,
		CORE_TEXT_TARGET_RATIO,
		EXTENDED_MIX_TEXT_TARGET_RATIO,
		'lighter',
	);
	light['--typo-anchor--color-light'] = formatOklch(typoAnchorLight);
	light['--typo-anchor--color-dark'] = formatOklch(typoAnchorDark);

	return { light, dark };
}

/**
 * Resolve the complete mode-specific custom property map for a Skeleton theme
 * (merging default root fallbacks, raw theme tokens, and calibrated light/dark overrides).
 * Suitable for downstream token/contrast test suites (e.g., `contrast.spec.ts`).
 */
export function resolveCalibratedThemeTokens(
	rawVars: Readonly<Record<string, string>>,
	mode: 'light' | 'dark',
): Record<string, string> {
	const calibrated = calibrateThemeTokens(rawVars);
	return mode === 'dark'
		? { ...DEFAULT_SKELETON_ROOT_TOKENS, ...rawVars, ...calibrated.light, ...calibrated.dark }
		: { ...DEFAULT_SKELETON_ROOT_TOKENS, ...rawVars, ...calibrated.light };
}

/**
 * Resolve the complete mode-specific custom property map from a raw Skeleton theme CSS file.
 */
export function resolveCalibratedThemeStylesheetVars(
	rawStylesheet: string,
	mode: 'light' | 'dark',
): Record<string, string> {
	return resolveCalibratedThemeTokens(extractCssVariables(rawStylesheet), mode);
}

/**
 * Parse an emitted calibrated theme stylesheet (such as `await loadThemeCss(theme)`)
 * into effective custom properties for `light` or `dark` mode, applying `.dark`
 * rule blocks only when `mode === 'dark'`.
 */
export function parseEmittedThemeStylesheetVars(
	emittedStylesheet: string,
	mode: 'light' | 'dark',
): Record<string, string> {
	const vars: Record<string, string> = { ...DEFAULT_SKELETON_ROOT_TOKENS };
	const blockRe = /([^{}]+)\{([^{}]*)\}/g;
	for (const match of emittedStylesheet.matchAll(blockRe)) {
		const selector = match[1].trim();
		const isDarkSelector = /\.dark\b/.test(selector);
		if (isDarkSelector && mode !== 'dark') continue;
		const body = match[2];
		for (const decl of body.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;{}]+);/g)) {
			vars[decl[1].trim()] = decl[2].trim();
		}
	}
	return vars;
}

function serializeRuleBlock(selector: string, declarations: CalibratedModeTokens, indent = ''): string {
	const entries = Object.entries(declarations);
	if (entries.length === 0) return '';
	const body = entries.map(([prop, val]) => `${indent}\t${prop}: ${val};`).join('\n');
	return `${indent}${selector} {\n${body}\n${indent}}`;
}

/** Build `:root[data-theme="<theme>"]` and `.dark` override CSS blocks for a Skeleton theme. */
export function buildCalibratedThemeOverrideCss(
	theme: string,
	rawVars: Readonly<Record<string, string>>,
): string {
	const calibrated = calibrateThemeTokens(rawVars);
	const lightBlock = serializeRuleBlock(`:root[data-theme="${theme}"]`, calibrated.light);
	const darkBlock = serializeRuleBlock(`:root[data-theme="${theme}"].dark`, calibrated.dark);
	return darkBlock ? `${lightBlock}\n\n${darkBlock}` : lightBlock;
}

/** Parse an upstream Skeleton theme stylesheet and return ONLY its calibrated override CSS block. */
export function getCalibratedThemeOverrides(theme: string, stylesheet: string): string {
	const rawVars = extractCssVariables(stylesheet);
	return buildCalibratedThemeOverrideCss(theme, rawVars);
}

/**
 * Elevate `[data-theme="<theme>"]` selector specificity to beat unlayered `:root` rules
 * during SSR `<svelte:head>` rendering and append calibrated light/dark overrides.
 */
export function calibrateThemeStylesheet(theme: string, stylesheet: string): string {
	const elevated = stylesheet.includes(`:root[data-theme="${theme}"], [data-theme="${theme}"]`)
		? stylesheet
		: stylesheet.replace(
				`[data-theme="${theme}"]`,
				`:root[data-theme="${theme}"], [data-theme="${theme}"]`,
			);
	const overrides = getCalibratedThemeOverrides(theme, stylesheet);
	return `${elevated}\n\n${overrides}\n`;
}

export const SHARED_TONAL_DESCENDANT_CSS = `/* Shared tonal foreground inheritance to prevent WCAG 2.2 Failure F24. */
.preset-tonal-primary {
	--bookward-tonal-fg: var(--color-primary-950-50);
}
.preset-tonal-secondary {
	--bookward-tonal-fg: var(--color-secondary-950-50);
}
.preset-tonal-tertiary {
	--bookward-tonal-fg: var(--color-tertiary-950-50);
}
.preset-tonal-success {
	--bookward-tonal-fg: var(--color-success-950-50);
}
.preset-tonal-warning {
	--bookward-tonal-fg: var(--color-warning-950-50);
}
.preset-tonal-error {
	--bookward-tonal-fg: var(--color-error-950-50);
}

:is(
	.preset-tonal-surface,
	.preset-tonal,
	.preset-filled,
	[class*="preset-filled-"],
	.input,
	.select,
	.textarea
) {
	--bookward-tonal-fg: initial;
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) :where(code) {
	color: var(--bookward-tonal-fg, var(--color-surface-950-50));
	background-color: transparent;
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) :is(.h1, .h2, .h3, .h4, .h5, .h6) {
	color: var(--bookward-tonal-fg, inherit);
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
):is(.text-surface-950-50),
:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) .text-surface-950-50:not(.preset-tonal-surface, .preset-tonal, .preset-filled, [class*="preset-filled-"], .input, .select, .textarea) {
	color: var(--bookward-tonal-fg, var(--color-surface-950-50));
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
):is(.text-surface-900-100),
:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) .text-surface-900-100:not(.preset-tonal-surface, .preset-tonal, .preset-filled, [class*="preset-filled-"], .input, .select, .textarea) {
	color: var(--bookward-tonal-fg, var(--color-surface-900-100));
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
):is(.text-surface-800-200),
:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) .text-surface-800-200:not(.preset-tonal-surface, .preset-tonal, .preset-filled, [class*="preset-filled-"], .input, .select, .textarea) {
	color: var(--bookward-tonal-fg, var(--color-surface-800-200));
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
):is(.text-surface-700-300),
:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) .text-surface-700-300:not(.preset-tonal-surface, .preset-tonal, .preset-filled, [class*="preset-filled-"], .input, .select, .textarea) {
	color: var(--bookward-tonal-fg, var(--color-surface-700-300));
}

:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
):is(.text-surface-600-400),
:is(
	.preset-tonal-primary,
	.preset-tonal-secondary,
	.preset-tonal-tertiary,
	.preset-tonal-success,
	.preset-tonal-warning,
	.preset-tonal-error
) .text-surface-600-400:not(.preset-tonal-surface, .preset-tonal, .preset-filled, [class*="preset-filled-"], .input, .select, .textarea) {
	color: var(--bookward-tonal-fg, var(--color-surface-600-400));
}`;

/**
 * Build the critical `theme-calibration.css` stylesheet containing calibrated `:root`
 * defaults, `:root[data-theme="cerberus"]` rules, and shared tonal descendant rules.
 */
export function buildCriticalCalibrationCss(cerberusStylesheet: string): string {
	const rootCalibrated = calibrateThemeTokens(DEFAULT_SKELETON_ROOT_TOKENS);
	const rootLightBlock = serializeRuleBlock(':root', rootCalibrated.light, '\t');
	const rootDarkBlock = serializeRuleBlock(':root.dark', rootCalibrated.dark, '\t');
	const cerberusOverrides = getCalibratedThemeOverrides('cerberus', cerberusStylesheet);

	return `/* Auto-calibrated WCAG 2.2 SC 1.4.3 / G18 tokens for :root and cerberus. */
@layer base {
${rootLightBlock}

${rootDarkBlock}
}

${cerberusOverrides}

${SHARED_TONAL_DESCENDANT_CSS}
`;
}
