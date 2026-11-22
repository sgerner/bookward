/**
 * OKLCH color parsing, WCAG 2.2 Technique G18 relative luminance, and
 * contrast calibration for Skeleton v5.0.4 theme stylesheets.
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

function mixOklab(base: OklchColor, overlay: OklchColor, weight: number): OklchColor {
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
		// Round-trip through formatted precision to verify rounded output also passes.
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
	/** Tokens set on `:root[data-theme="<id>"]` (light mode + shared base scale). */
	light: CalibratedModeTokens;
	/** Mode-specific overrides set on `:root[data-theme="<id>"].dark`. */
	dark: CalibratedModeTokens;
};

const TEXT_TARGET_RATIO = 4.9;
const FILLED_TARGET_RATIO = 4.7;
const TONAL_TARGET_RATIO = 5.0;
const SWITCH_TARGET_RATIO = 3.1;

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

	// `c500` sits in the narrow mid-luminance deadband (~0.175..0.205). Shift `c500.L` minimally
	// in the direction that preserves `surfaceBg` >= 3.1:1 switch boundary contrast.
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

	// Normalize root backgrounds so pages using `html` background remain genuinely light/dark.
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

	// Include subtle card/modal tint mixes (5-8%) in the surface background envelope so translucent
	// card gradients (`from-primary-500/8`, `bg-surface-950/5`) never push text below 4.5:1.
	const rawPrimary500 = colorOf('--color-primary-500');
	const rawSecondary500 = colorOf('--color-secondary-500');
	const lightBackgrounds: OklchColor[] = [
		surface50,
		rootBgLight,
		mixOklab(surface50, surface950, 0.06),
		mixOklab(surface50, rawPrimary500, 0.08),
		mixOklab(surface50, rawSecondary500, 0.08),
	];
	const darkBackgrounds: OklchColor[] = [
		surface950,
		rootBgDark,
		mixOklab(surface950, surface50, 0.05),
		mixOklab(surface950, rawPrimary500, 0.08),
		mixOklab(surface950, rawSecondary500, 0.08),
	];

	// Build a strictly monotonic, contrast-calibrated 11-step surface scale.
	// Check if upstream surface scale is inverted or compressed.
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

	const origSurfaceContrast500 = colorOf('--color-surface-contrast-500');
	const initialLightSurfaceFilled = calibrateFilledPairForMode(
		calibratedSurface['500'],
		origSurfaceContrast500,
		surface50,
		surface950,
		surface50,
		'light',
		true,
	);
	const initialDarkSurfaceFilled = calibrateFilledPairForMode(
		calibratedSurface['500'],
		origSurfaceContrast500,
		surface50,
		surface950,
		surface950,
		'dark',
		true,
	);

	// Calibrate light-mode surface text shades (600, 700, 800, 900, 950) with clear hierarchy.
	calibratedSurface['600'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['600'],
			L: Math.min(calibratedSurface['600'].L, initialLightSurfaceFilled.c500.L - 0.02),
		},
		lightBackgrounds,
		TEXT_TARGET_RATIO,
		'darker',
	);
	calibratedSurface['700'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['700'],
			L: Math.min(calibratedSurface['700'].L, calibratedSurface['600'].L - 0.03),
		},
		lightBackgrounds,
		5.6,
		'darker',
	);
	calibratedSurface['800'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['800'],
			L: Math.min(calibratedSurface['800'].L, calibratedSurface['700'].L - 0.04),
		},
		lightBackgrounds,
		7.0,
		'darker',
	);
	calibratedSurface['900'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['900'],
			L: Math.min(calibratedSurface['900'].L, calibratedSurface['800'].L - 0.04),
		},
		lightBackgrounds,
		8.5,
		'darker',
	);
	calibratedSurface['950'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['950'],
			L: Math.min(calibratedSurface['950'].L, calibratedSurface['900'].L - 0.02),
		},
		lightBackgrounds,
		10.0,
		'darker',
	);
	surface950 = calibratedSurface['950'];

	// Calibrate dark-mode surface text shades (400, 300, 200, 100, 50) with clear hierarchy.
	const maxSurface500L = Math.max(initialLightSurfaceFilled.c500.L, initialDarkSurfaceFilled.c500.L);
	calibratedSurface['400'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['400'],
			L: Math.max(calibratedSurface['400'].L, maxSurface500L + 0.02),
		},
		darkBackgrounds,
		TEXT_TARGET_RATIO,
		'lighter',
	);
	calibratedSurface['300'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['300'],
			L: Math.max(calibratedSurface['300'].L, calibratedSurface['400'].L + 0.04),
		},
		darkBackgrounds,
		5.8,
		'lighter',
	);
	calibratedSurface['200'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['200'],
			L: Math.max(calibratedSurface['200'].L, calibratedSurface['300'].L + 0.05),
		},
		darkBackgrounds,
		7.5,
		'lighter',
	);
	calibratedSurface['100'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['100'],
			L: Math.max(calibratedSurface['100'].L, calibratedSurface['200'].L + 0.05),
		},
		darkBackgrounds,
		9.5,
		'lighter',
	);
	calibratedSurface['50'] = calibrateLightnessForContrast(
		{
			...calibratedSurface['50'],
			L: Math.max(calibratedSurface['50'].L, calibratedSurface['100'].L + 0.02),
		},
		darkBackgrounds,
		11.5,
		'lighter',
	);
	surface50 = calibratedSurface['50'];

	const surfaceContrast50 = calibrateLightnessForContrast(
		colorOf('--color-surface-contrast-50'),
		lightBackgrounds,
		9.0,
		'darker',
	);
	const surfaceContrast950 = calibrateLightnessForContrast(
		colorOf('--color-surface-contrast-950'),
		darkBackgrounds,
		9.0,
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

	light['--color-surface-700-300'] = formatOklch(calibratedSurface['700']);
	dark['--color-surface-700-300'] = formatOklch(calibratedSurface['300']);
	light['--color-surface-800-200'] = formatOklch(calibratedSurface['800']);
	dark['--color-surface-800-200'] = formatOklch(calibratedSurface['200']);
	light['--color-surface-900-100'] = formatOklch(calibratedSurface['900']);
	dark['--color-surface-900-100'] = formatOklch(calibratedSurface['100']);

	// 2. Calibrate all 7 roles: tonal (50/950), link/muted (600/400), and filled (500/contrast-500).
	const switchRoles = new Set<ThemeColorRole>(['primary', 'secondary', 'tertiary', 'surface']);

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
			// Also ensure dark-mode tonal fg (c50) reaches >= 5.0:1 on c950.
			c50 = calibrateLightnessForContrast(c50, [c950], TONAL_TARGET_RATIO, 'lighter');

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
				: calibrateLightnessForContrast(
						colorOf(`--color-${role}-600`),
						lightBackgrounds,
						TEXT_TARGET_RATIO,
						'darker',
					);
		const c400 =
			role === 'surface'
				? calibratedSurface['400']
				: calibrateLightnessForContrast(
						colorOf(`--color-${role}-400`),
						darkBackgrounds,
						TEXT_TARGET_RATIO,
						'lighter',
					);

		if (role !== 'surface') {
			light[`--color-${role}-600`] = formatOklch(c600);
			light[`--color-${role}-400`] = formatOklch(c400);
		}
		light[`--color-${role}-600-400`] = formatOklch(c600);
		dark[`--color-${role}-600-400`] = formatOklch(c400);

		const orig500 = role === 'surface' ? calibratedSurface['500'] : colorOf(`--color-${role}-500`);
		const origContrast500 = colorOf(`--color-${role}-contrast-500`);
		const needsSwitchContrast = switchRoles.has(role);

		const lightFilled = calibrateFilledPairForMode(
			orig500,
			origContrast500,
			c50,
			c950,
			surface50,
			'light',
			needsSwitchContrast,
		);
		const darkFilled = calibrateFilledPairForMode(
			orig500,
			origContrast500,
			c50,
			c950,
			surface950,
			'dark',
			needsSwitchContrast,
		);

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

	// 3. Calibrate base, heading, and anchor typography tokens against surface & root backgrounds.
	const typoBaseLight = calibrateLightnessForContrast(
		colorOf('--typo-base--color-light'),
		lightBackgrounds,
		9.0,
		'darker',
	);
	const typoBaseDark = calibrateLightnessForContrast(
		colorOf('--typo-base--color-dark'),
		darkBackgrounds,
		9.0,
		'lighter',
	);
	light['--typo-base--color-light'] = formatOklch(typoBaseLight);
	light['--typo-base--color-dark'] = formatOklch(typoBaseDark);

	const rawHeadingLight = resolveTokenValue('--typo-heading--color-light', rawVars);
	if (rawHeadingLight && rawHeadingLight !== 'inherit') {
		light['--typo-heading--color-light'] = formatOklch(
			calibrateLightnessForContrast(
				parseCssColor(rawHeadingLight),
				lightBackgrounds,
				TEXT_TARGET_RATIO,
				'darker',
			),
		);
	}
	const rawHeadingDark = resolveTokenValue('--typo-heading--color-dark', rawVars);
	if (rawHeadingDark && rawHeadingDark !== 'inherit') {
		light['--typo-heading--color-dark'] = formatOklch(
			calibrateLightnessForContrast(
				parseCssColor(rawHeadingDark),
				darkBackgrounds,
				TEXT_TARGET_RATIO,
				'lighter',
			),
		);
	}

	const typoAnchorLight = calibrateLightnessForContrast(
		colorOf('--typo-anchor--color-light'),
		lightBackgrounds,
		TEXT_TARGET_RATIO,
		'darker',
	);
	const typoAnchorDark = calibrateLightnessForContrast(
		colorOf('--typo-anchor--color-dark'),
		darkBackgrounds,
		TEXT_TARGET_RATIO,
		'lighter',
	);
	light['--typo-anchor--color-light'] = formatOklch(typoAnchorLight);
	light['--typo-anchor--color-dark'] = formatOklch(typoAnchorDark);

	return { light, dark };
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

/** Append calibrated `:root[data-theme="<theme>"]` overrides to a raw Skeleton theme stylesheet. */
export function calibrateThemeStylesheet(theme: string, stylesheet: string): string {
	const overrides = getCalibratedThemeOverrides(theme, stylesheet);
	return `${stylesheet}\n\n${overrides}\n`;
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
