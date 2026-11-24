import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { defineConfig, type Plugin } from 'vitest/config';
import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { calibrateThemeStylesheet } from './src/lib/server/theme-contrast.js';

const SKELETON_THEME_SPECIFIER_RE = /^@skeletonlabs\/skeleton\/themes\/([a-z0-9-]+)\?raw$/;
const SKELETON_RAW_THEME_PATH_RE =
	/[/\\]@skeletonlabs[/\\]skeleton[/\\]src[/\\]themes[/\\]([a-z0-9-]+)\.css\?raw(?:&.*)?$/;
const VIRTUAL_THEME_PREFIX = '\0bookward-calibrated-theme:';

function calibratedSkeletonThemes(): Plugin {
	const cache = new Map<string, string>();
	return {
		name: 'bookward-calibrated-skeleton-themes',
		enforce: 'pre',
		resolveId(source) {
			const specMatch = source.match(SKELETON_THEME_SPECIFIER_RE);
			if (specMatch) return `${VIRTUAL_THEME_PREFIX}${specMatch[1]}`;
			const pathMatch = source.match(SKELETON_RAW_THEME_PATH_RE);
			if (pathMatch) return `${VIRTUAL_THEME_PREFIX}${pathMatch[1]}`;
			return null;
		},
		load(id) {
			if (!id.startsWith(VIRTUAL_THEME_PREFIX)) return null;
			const theme = id.slice(VIRTUAL_THEME_PREFIX.length);
			const cached = cache.get(theme);
			if (cached) return cached;
			const filePath = join(
				process.cwd(),
				'node_modules/@skeletonlabs/skeleton/src/themes',
				`${theme}.css`,
			);
			this.addWatchFile(filePath);
			const rawCss = readFileSync(filePath, 'utf8');
			const calibratedCss = calibrateThemeStylesheet(theme, rawCss);
			const moduleCode = `export default ${JSON.stringify(calibratedCss)};`;
			cache.set(theme, moduleCode);
			return moduleCode;
		}
	};
}

export default defineConfig({
	plugins: [
		calibratedSkeletonThemes(),
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) => filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			adapter: adapter()
		})
	],
	test: {
		expect: { requireAssertions: true },
		projects: [
			{
				extends: './vite.config.ts',
				test: {
					name: 'server',
					environment: 'node',
					include: ['src/**/*.{test,spec}.{js,ts}'],
					exclude: ['src/**/*.svelte.{test,spec}.{js,ts}']
				}
			}
		]
	}
});
