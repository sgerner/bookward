import type { LayoutServerLoad } from './$types';
import {
	DEFAULT_MODE,
	DEFAULT_THEME,
	MODE_STORAGE_KEY,
	THEME_STORAGE_KEY,
	getThemeColor,
	isThemeMode,
	isThemeName,
	loadThemeCss,
} from '#lib/theme.js';

export const load: LayoutServerLoad = async ({ locals, cookies }) => {
	const cookieTheme = cookies.get(THEME_STORAGE_KEY);
	const cookieMode = cookies.get(MODE_STORAGE_KEY);
	let theme = isThemeName(cookieTheme) ? cookieTheme : DEFAULT_THEME;
	const mode = isThemeMode(cookieMode) ? cookieMode : DEFAULT_MODE;
	let themeCss: string | null = null;
	if (theme !== DEFAULT_THEME) {
		try {
			const loaded = await loadThemeCss(theme);
			if (loaded) themeCss = loaded;
			else theme = DEFAULT_THEME;
		} catch {
			theme = DEFAULT_THEME;
		}
	}
	const resolvedMode = mode === 'light' ? 'light' : 'dark';
	return {
		user: locals.user ?? null,
		theme,
		mode,
		themeCss,
		themeColor: getThemeColor(theme, resolvedMode),
	};
};
