import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const rootDir = process.cwd();
const dockerfilePath = join(rootDir, 'Dockerfile');
const dockerignorePath = join(rootDir, '.dockerignore');
const hasDockerConfig = existsSync(dockerfilePath) && existsSync(dockerignorePath);

function readConfigLines(filePath: string): string[] {
	if (!existsSync(filePath)) return [];
	return readFileSync(filePath, 'utf8')
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line.length > 0 && !line.startsWith('#'));
}

function globToRegExp(pattern: string): RegExp {
	const normalized = pattern.replace(/\/+$/, '');
	if (normalized.startsWith('**/')) {
		const suffix = normalized
			.slice(3)
			.replace(/[.+^${}()|[\]\\]/g, '\\$&')
			.replace(/\*/g, '[^/]*');
		return new RegExp(`(?:^|/)${suffix}(?:/|$)`);
	}
	const escaped = normalized
		.replace(/[.+^${}()|[\]\\]/g, '\\$&')
		.replace(/\*/g, '[^/]*');
	return new RegExp(`^${escaped}(?:/|$)`);
}

function isIgnoredByDockerignore(relativePath: string, rules: string[]): boolean {
	let ignored = false;
	for (const rule of rules) {
		const negated = rule.startsWith('!');
		const pattern = negated ? rule.slice(1).trim() : rule;
		if (!pattern) continue;
		if (globToRegExp(pattern).test(relativePath)) {
			ignored = !negated;
		}
	}
	return ignored;
}

function parseStageInstructions(dockerfileLines: string[], stageName: string): string[] {
	const stages = new Map<string, string[]>();
	let currentStage: string | null = null;

	for (const line of dockerfileLines) {
		const fromMatch = /^FROM\s+\S+(?:\s+AS\s+(\S+))?$/i.exec(line);
		if (fromMatch) {
			currentStage = fromMatch[1] ?? '';
			stages.set(currentStage, []);
			continue;
		}
		if (currentStage !== null) {
			stages.get(currentStage)?.push(line);
		}
	}

	return stages.get(stageName) ?? [];
}

describe.skipIf(!hasDockerConfig)('root Docker web build configuration', () => {
	const dockerignoreRules = readConfigLines(dockerignorePath);
	const dockerfileLines = readConfigLines(dockerfilePath);

	it('excludes non-web paths in .dockerignore while keeping web build inputs', () => {
		const requiredRules = [
			'engine',
			'docs',
			'scripts',
			'.github',
			'.vscode',
			'.gitignore',
			'*.md',
			'compose*.yaml',
			'Dockerfile*',
			'.dockerignore',
		];
		for (const rule of requiredRules) {
			expect(dockerignoreRules, `.dockerignore should contain ${rule}`).toContain(rule);
		}

		const excludedPaths = [
			'engine',
			'engine/afterword_engine/main.py',
			'docs',
			'docs/reading-workflow.md',
			'scripts',
			'scripts/community_smoke.py',
			'.github',
			'.github/workflows/ci.yml',
			'.vscode',
			'.gitignore',
			'.stitch.json',
			'README.md',
			'SECURITY.md',
			'compose.yaml',
			'compose.gpu.yaml',
			'Dockerfile',
			'.dockerignore',
			'node_modules',
			'.svelte-kit',
			'build',
			'.env',
			'.env.example',
			'.git',
		];
		for (const candidate of excludedPaths) {
			expect(
				isIgnoredByDockerignore(candidate, dockerignoreRules),
				`${candidate} should be excluded from the root Docker context`,
			).toBe(true);
		}

		const keptWebInputs = [
			'package.json',
			'package-lock.json',
			'.npmrc',
			'tsconfig.json',
			'vite.config.ts',
			'static',
			'static/robots.txt',
			'src',
			'src/app.html',
		];
		for (const input of keptWebInputs) {
			expect(
				isIgnoredByDockerignore(input, dockerignoreRules),
				`${input} should remain in the root Docker context`,
			).toBe(false);
		}
	});

	it('copies explicit web inputs in the build stage without COPY . .', () => {
		const buildStage = parseStageInstructions(dockerfileLines, 'build');
		expect(buildStage.length).toBeGreaterThan(0);

		expect(buildStage).not.toContain('COPY . .');

		const manifestCopyIndex = buildStage.findIndex(
			(line) => line.startsWith('COPY ') && line.includes('package*.json') && line.includes('.npmrc'),
		);
		const npmCiIndex = buildStage.indexOf('RUN npm ci');
		const buildRunIndex = buildStage.indexOf('RUN npm run check && npm run build');

		expect(manifestCopyIndex).toBeGreaterThanOrEqual(0);
		expect(npmCiIndex).toBeGreaterThan(manifestCopyIndex);
		expect(buildRunIndex).toBeGreaterThan(npmCiIndex);

		const copyBeforeBuild = buildStage.slice(npmCiIndex + 1, buildRunIndex);
		for (const requiredInput of ['tsconfig.json', 'vite.config.ts', 'static', 'src']) {
			expect(
				copyBeforeBuild.some((line) => line.startsWith('COPY ') && line.includes(requiredInput)),
				`build stage should explicitly copy ${requiredInput} after npm ci`,
			).toBe(true);
		}
	});

	it('installs production dependencies before copying build output in the runtime stage', () => {
		const runtimeStage = parseStageInstructions(dockerfileLines, 'runtime');
		expect(runtimeStage.length).toBeGreaterThan(0);

		const manifestCopyIndex = runtimeStage.findIndex(
			(line) => line.startsWith('COPY ') && line.includes('package*.json') && line.includes('.npmrc'),
		);
		const prodInstallIndex = runtimeStage.indexOf('RUN npm ci --omit=dev --ignore-scripts');
		const buildCopyIndex = runtimeStage.indexOf(
			'COPY --chown=node:node --from=build /app/build ./build',
		);
		const userNodeIndex = runtimeStage.indexOf('USER node');

		expect(manifestCopyIndex).toBeGreaterThanOrEqual(0);
		expect(prodInstallIndex).toBeGreaterThan(manifestCopyIndex);
		expect(buildCopyIndex).toBeGreaterThan(prodInstallIndex);
		expect(userNodeIndex).toBeGreaterThan(buildCopyIndex);
		expect(runtimeStage.some((line) => line.includes('chown -R'))).toBe(false);
	});
});
