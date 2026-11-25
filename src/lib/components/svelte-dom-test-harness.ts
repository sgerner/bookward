import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type { Readable, Writable } from 'node:stream';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { build, type Plugin, type Rollup } from 'vite';

const CHROME_CANDIDATES = [
	process.env.CHROME_BIN,
	'/usr/bin/google-chrome',
	'/usr/bin/google-chrome-stable',
	'/usr/bin/chromium',
	'/usr/bin/chromium-browser',
	'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter((entry): entry is string => Boolean(entry));

export function resolveChromeBinary(): string {
	for (const candidate of CHROME_CANDIDATES) {
		if (existsSync(candidate)) return candidate;
	}
	throw new Error(
		`No Chrome/Chromium binary found for Svelte DOM tests (checked: ${CHROME_CANDIDATES.join(', ')})`,
	);
}

type VirtualFiles = Record<string, string>;

const SVELTEKIT_SHIMS: Record<string, string> = {
	'$app/state': `
		export const page = {
			url: new URL('http://bookward.test/'),
			params: {},
			route: { id: '/' },
			status: 200,
			error: null,
			data: {},
			form: null,
			state: {},
		};
	`,
	'$app/navigation': `
		export async function invalidateAll() {}
		export function pushState(url, state) {
			if (typeof window !== 'undefined' && url) {
				window.history.pushState(state ?? {}, '', String(url));
			}
		}
		export async function goto() {}
	`,
	'$app/forms': `
		export function enhance() {
			return { destroy() {} };
		}
		export async function applyAction() {}
		export function deserialize(text) {
			return JSON.parse(text);
		}
	`,
};

export async function compileSvelteClientBundle(options: {
	entryFile: string;
	files: VirtualFiles;
	includeTailwindAndLayoutCss?: boolean;
}): Promise<{ js: string; css: string }> {
	const rootDir = process.cwd();
	const virtualMap = new Map<string, string>();
	for (const [relPath, code] of Object.entries(options.files)) {
		virtualMap.set(join(rootDir, relPath), code);
	}
	const entryAbsPath = join(rootDir, options.entryFile);

	const virtualPlugin: Plugin = {
		name: 'bookward-virtual-svelte-dom-harness',
		enforce: 'pre',
		resolveId(source, importer) {
			if (source in SVELTEKIT_SHIMS) {
				return `\0shim:${source}`;
			}
			if (source.endsWith('.svg')) {
				return '\0shim:svg';
			}
			if (source.endsWith('.css?raw')) {
				return '\0shim:css-raw';
			}
			if (virtualMap.has(source)) {
				return source;
			}
			if (importer && source.startsWith('.')) {
				const importerDir = importer.slice(0, Math.max(0, importer.lastIndexOf('/')));
				const candidate = join(importerDir, source);
				if (virtualMap.has(candidate)) {
					return candidate;
				}
			}
			return null;
		},
		load(id) {
			if (id.startsWith('\0shim:$app/')) {
				return SVELTEKIT_SHIMS[id.slice('\0shim:'.length)];
			}
			if (id === '\0shim:svg') {
				return 'export default "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27/%3E";';
			}
			if (id === '\0shim:css-raw') {
				return 'export default "";';
			}
			const virtualCode = virtualMap.get(id);
			if (virtualCode !== undefined) {
				return virtualCode;
			}
			return null;
		},
	};

	const plugins: Plugin[] = [
		virtualPlugin,
		...(svelte({
			compilerOptions: {
				dev: false,
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true,
			},
		}) as Plugin[]),
	];

	const result = await build({
		root: rootDir,
		configFile: false,
		logLevel: 'silent',
		plugins,
		resolve: {
			conditions: ['browser', 'import', 'module', 'default'],
		},
		build: {
			write: false,
			minify: false,
			target: 'esnext',
			rollupOptions: {
				input: entryAbsPath,
				output: {
					format: 'iife',
					name: '__BookwardTestBundle',
				},
			},
		},
	});

	const outputs = Array.isArray(result) ? result : [result];
	let js = '';
	let css = '';
	for (const output of outputs) {
		if (!('output' in output)) continue;
		for (const chunk of (output as Rollup.RollupOutput).output) {
			if (chunk.type === 'chunk') {
				js += `${chunk.code}\n`;
			} else if (typeof chunk.fileName === 'string' && chunk.fileName.endsWith('.css')) {
				css += `${String(chunk.source)}\n`;
			}
		}
	}
	return { js, css };
}

type CdpMessage = {
	id?: number;
	sessionId?: string;
	method?: string;
	params?: Record<string, unknown>;
	result?: Record<string, unknown>;
	error?: { code?: number; message?: string };
};

export type AxSummary = {
	role: string;
	name: string;
	description: string;
	checked?: string;
	expanded?: boolean;
};

export class CdpBrowserSession {
	private proc: ChildProcess;
	private pipeWrite: Writable;
	private nextId = 1;
	private pending = new Map<
		number,
		{ resolve: (value: Record<string, unknown>) => void; reject: (err: Error) => void }
	>();
	private listeners = new Map<string, Set<(params: Record<string, unknown>) => void>>();
	private sessionId = '';

	private constructor(proc: ChildProcess, pipeWrite: Writable, pipeRead: Readable) {
		this.proc = proc;
		this.pipeWrite = pipeWrite;

		let buffer = '';
		pipeRead.setEncoding('utf8');
		pipeRead.on('data', (chunk: string) => {
			buffer += chunk;
			let nullIdx = buffer.indexOf('\0');
			while (nullIdx !== -1) {
				const raw = buffer.slice(0, nullIdx);
				buffer = buffer.slice(nullIdx + 1);
				if (raw.trim()) {
					this.handleMessage(JSON.parse(raw) as CdpMessage);
				}
				nullIdx = buffer.indexOf('\0');
			}
		});
		proc.on('exit', () => {
			for (const [, waiter] of this.pending) {
				waiter.reject(new Error('Chrome process exited unexpectedly'));
			}
			this.pending.clear();
		});
	}

	static async launch(viewport = { width: 1280, height: 900 }): Promise<CdpBrowserSession> {
		const chromeBin = resolveChromeBinary();
		const proc = spawn(
			chromeBin,
			[
				'--headless',
				'--no-sandbox',
				'--disable-gpu',
				'--disable-dev-shm-usage',
				'--disable-background-networking',
				'--force-prefers-reduced-motion',
				'--remote-debugging-pipe',
				'about:blank',
			],
			{
				stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'],
			},
		);
		const pipeWrite = proc.stdio[3] as Writable;
		const pipeRead = proc.stdio[4] as Readable;
		const session = new CdpBrowserSession(proc, pipeWrite, pipeRead);
		await session.init(viewport);
		return session;
	}

	private handleMessage(msg: CdpMessage) {
		if (typeof msg.id === 'number') {
			const waiter = this.pending.get(msg.id);
			if (!waiter) return;
			this.pending.delete(msg.id);
			if (msg.error) {
				waiter.reject(new Error(msg.error.message ?? 'CDP command failed'));
			} else {
				waiter.resolve(msg.result ?? {});
			}
			return;
		}
		if (msg.method) {
			const handlers = this.listeners.get(msg.method);
			if (handlers) {
				for (const fn of handlers) {
					fn(msg.params ?? {});
				}
			}
		}
	}

	private on(method: string, handler: (params: Record<string, unknown>) => void) {
		if (!this.listeners.has(method)) {
			this.listeners.set(method, new Set());
		}
		this.listeners.get(method)!.add(handler);
	}

	async send(
		method: string,
		params: Record<string, unknown> = {},
		useSession = true,
	): Promise<Record<string, unknown>> {
		const id = this.nextId++;
		const payload: Record<string, unknown> = { id, method, params };
		if (useSession && this.sessionId) {
			payload.sessionId = this.sessionId;
		}
		return new Promise((resolve, reject) => {
			this.pending.set(id, { resolve, reject });
			this.pipeWrite.write(`${JSON.stringify(payload)}\0`);
		});
	}

	private async init(viewport: { width: number; height: number }) {
		const created = await this.send(
			'Target.createTarget',
			{ url: 'about:blank' },
			false,
		);
		const attached = await this.send(
			'Target.attachToTarget',
			{ targetId: created.targetId, flatten: true },
			false,
		);
		this.sessionId = String(attached.sessionId);

		this.on('Fetch.requestPaused', (params) => {
			const requestId = String(params.requestId ?? '');
			const request = (params.request ?? {}) as { url?: string };
			const url = String(request.url ?? '');
			const isRootDoc = url === 'http://bookward.test/' || url === 'http://bookward.test';
			const body = isRootDoc
				? '<!doctype html><html lang="en"><head><meta charset="utf-8"><style>.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border-width:0}</style></head><body><div id="app"></div></body></html>'
				: '{}';
			const contentType = isRootDoc
				? 'text/html; charset=utf-8'
				: 'application/json; charset=utf-8';
			void this.send('Fetch.fulfillRequest', {
				requestId,
				responseCode: 200,
				responseHeaders: [{ name: 'content-type', value: contentType }],
				body: Buffer.from(body, 'utf8').toString('base64'),
			}).catch(() => {});
		});

		await this.send('Fetch.enable', {
			patterns: [{ urlPattern: 'http://bookward.test*' }, { urlPattern: 'https://*' }],
		});
		await this.send('Page.enable');
		await this.send('Runtime.enable');
		await this.send('DOM.enable');
		await this.send('Accessibility.enable');
		await this.setViewport(viewport.width, viewport.height);
	}

	async setViewport(width: number, height: number) {
		await this.send('Emulation.setDeviceMetricsOverride', {
			width,
			height,
			deviceScaleFactor: 1,
			mobile: width < 640,
		});
	}

	async loadBundle(bundle: { js: string; css: string }) {
		await this.send('Page.navigate', { url: 'http://bookward.test/' });
		await this.evaluate(`
			new Promise((resolve) => {
				if (document.readyState === 'complete' || document.readyState === 'interactive') {
					resolve(true);
				} else {
					window.addEventListener('DOMContentLoaded', () => resolve(true), { once: true });
				}
			})
		`);
		if (bundle.css) {
			await this.evaluate(`
				(() => {
					const style = document.createElement('style');
					style.textContent = ${JSON.stringify(bundle.css)};
					document.head.appendChild(style);
				})()
			`);
		}
		await this.evaluate(bundle.js);
	}

	async evaluate<T = unknown>(expression: string): Promise<T> {
		const res = await this.send('Runtime.evaluate', {
			expression,
			awaitPromise: true,
			returnByValue: true,
		});
		if (res.exceptionDetails) {
			const details = res.exceptionDetails as {
				text?: string;
				exception?: { description?: string };
			};
			throw new Error(
				details.exception?.description ?? details.text ?? 'Runtime.evaluate failed',
			);
		}
		const resultObj = (res.result ?? {}) as { value?: T };
		return resultObj.value as T;
	}

	async mouseClickAt(x: number, y: number) {
		await this.send('Input.dispatchMouseEvent', {
			type: 'mousePressed',
			x,
			y,
			button: 'left',
			clickCount: 1,
		});
		await this.send('Input.dispatchMouseEvent', {
			type: 'mouseReleased',
			x,
			y,
			button: 'left',
			clickCount: 1,
		});
	}

	async getAxSummary(selector: string): Promise<AxSummary> {
		const doc = (await this.send('DOM.getDocument', { depth: 0 })) as {
			root: { nodeId: number };
		};
		const queried = (await this.send('DOM.querySelector', {
			nodeId: doc.root.nodeId,
			selector,
		})) as { nodeId: number };
		if (!queried.nodeId) {
			throw new Error(`Selector not found in DOM: ${selector}`);
		}
		const ax = (await this.send('Accessibility.getPartialAXTree', {
			nodeId: queried.nodeId,
			fetchRelatives: false,
		})) as {
			nodes?: Array<{
				ignored?: boolean;
				role?: { value?: string };
				name?: { value?: string };
				description?: { value?: string };
				properties?: Array<{ name?: string; value?: { value?: unknown } }>;
			}>;
		};
		const target = (ax.nodes ?? []).find((n) => !n.ignored) ?? (ax.nodes ?? [])[0];
		if (!target) {
			throw new Error(`No AX node returned for selector: ${selector}`);
		}
		const checkedProp = target.properties?.find((p) => p.name === 'checked')?.value?.value;
		const expandedProp = target.properties?.find((p) => p.name === 'expanded')?.value?.value;
		return {
			role: String(target.role?.value ?? ''),
			name: String(target.name?.value ?? ''),
			description: String(target.description?.value ?? ''),
			...(checkedProp !== undefined ? { checked: String(checkedProp) } : {}),
			...(typeof expandedProp === 'boolean' ? { expanded: expandedProp } : {}),
		};
	}

	async close() {
		if (this.proc.exitCode === null && !this.proc.killed) {
			this.proc.kill('SIGTERM');
			await new Promise<void>((resolve) => {
				const timer = setTimeout(() => {
					if (this.proc.exitCode === null) this.proc.kill('SIGKILL');
					resolve();
				}, 1000);
				this.proc.once('exit', () => {
					clearTimeout(timer);
					resolve();
				});
			});
		}
	}
}
