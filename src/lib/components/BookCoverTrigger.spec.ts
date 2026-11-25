import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRawSnippet } from 'svelte';
import { parse } from 'svelte/compiler';
import { render } from 'svelte/server';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import BookCoverTrigger from './BookCoverTrigger.svelte';
import { CdpBrowserSession, compileSvelteClientBundle } from './svelte-dom-test-harness';

const coverImageSnippet = createRawSnippet(() => ({
	render: () =>
		'<img src="https://covers.example/left-hand.jpg" alt="" width="160" height="240" />',
}));

type AstNode = {
	type?: string;
	name?: string;
	attributes?: Array<{
		type: string;
		name?: string;
		value?: true | Array<{ type?: string; data?: string }>;
	}>;
	[key: string]: unknown;
};

function collectNodes(node: unknown, predicate: (entry: AstNode) => boolean): AstNode[] {
	if (!node || typeof node !== 'object') return [];
	const record = node as AstNode;
	const found: AstNode[] = predicate(record) ? [record] : [];
	for (const [key, value] of Object.entries(record)) {
		if (key === 'attributes') continue;
		if (Array.isArray(value)) {
			for (const child of value) found.push(...collectNodes(child, predicate));
		} else if (value && typeof value === 'object') {
			found.push(...collectNodes(value, predicate));
		}
	}
	return found;
}

describe('BookCoverTrigger component and +page.svelte wiring', () => {
	it('names the cover button with the book title first and view details second while rendering Rank outside the button', () => {
		const { body } = render(BookCoverTrigger, {
			props: {
				id: 'cover-42',
				title: 'The Left Hand of Darkness',
				score: 94,
				hasCover: true,
				expanded: true,
				children: coverImageSnippet,
			},
		});

		expect(body).not.toContain('aria-label=');
		expect(body).toContain('aria-labelledby="cover-42-title cover-42-action"');
		expect(body).toContain('aria-expanded="true"');
		expect(body).toContain('alt=""');
		expect(body).toContain('pointer-events-none absolute inset-0 z-0');
		expect(body.indexOf('The Left Hand of Darkness')).toBeLessThan(
			body.indexOf('view details'),
		);

		const buttonCloseIndex = body.indexOf('</button>');
		const rankIndex = body.indexOf('Rank 94');
		expect(buttonCloseIndex).toBeGreaterThan(0);
		expect(rankIndex).toBeGreaterThan(buttonCloseIndex);
		expect(body).toContain('pointer-events-none');
	});

	it('hides the mobile backdrop when hasCover is false', () => {
		const collapsed = render(BookCoverTrigger, {
			props: {
				id: 'cover-7',
				title: 'Piranesi',
				score: 88,
				hasCover: false,
				expanded: false,
			},
		});
		expect(collapsed.body).toContain('class="hidden h-full w-full');
		expect(collapsed.body).toContain('aspect-[2/3] self-start');
		expect(collapsed.body).toContain('aria-expanded="false"');
	});

	it('wires BookCoverTrigger in +page.svelte with children cover image and without dead coverUrl/loading props', () => {
		const pageSource = readFileSync(
			fileURLToPath(new URL('../../routes/+page.svelte', import.meta.url)),
			'utf8',
		);
		const triggers = collectNodes(
			parse(pageSource, { modern: true }).fragment,
			(entry) => entry.type === 'Component' && entry.name === 'BookCoverTrigger',
		);
		expect(triggers).toHaveLength(1);

		const [trigger] = triggers;
		const attrNames = (trigger.attributes ?? []).map((attr) => attr.name);
		expect(attrNames).toEqual(
			expect.arrayContaining(['title', 'score', 'hasCover', 'expanded', 'onclick']),
		);
		expect(attrNames).not.toContain('coverUrl');
		expect(attrNames).not.toContain('loading');

		const nestedImgs = collectNodes(
			trigger,
			(entry) => entry.type === 'RegularElement' && entry.name === 'img',
		);
		expect(nestedImgs).toHaveLength(1);
		const altAttr = nestedImgs[0].attributes?.find((attr) => attr.name === 'alt');
		expect(altAttr).toBeDefined();
		expect(
			Array.isArray(altAttr?.value)
				? altAttr.value.map((part) => part.data ?? '').join('')
				: '',
		).toBe('');
	});
});

describe('BookCoverTrigger real Svelte 5 client DOM interactions', () => {
	let session: CdpBrowserSession;

	beforeAll(async () => {
		const bundle = await compileSvelteClientBundle({
			entryFile: 'src/lib/components/__virtual_cover_entry.ts',
			files: {
				'src/lib/components/__virtual_CoverHost.svelte': `
					<script lang="ts">
						import BookCoverTrigger from './BookCoverTrigger.svelte';

						let expanded = $state(false);
					</script>

					<div style="width: 160px; height: 240px; position: relative;">
						<BookCoverTrigger
							id="cover-42"
							title="The Left Hand of Darkness"
							score={94}
							hasCover={true}
							{expanded}
							onclick={() => (expanded = !expanded)}
						>
							<img
								src="data:image/gif;base64,R0lGODlhAQABAAAAACw="
								alt=""
								width="160"
								height="240"
							/>
						</BookCoverTrigger>
					</div>
					<output id="expanded-state">{String(expanded)}</output>
				`,
				'src/lib/components/__virtual_cover_entry.ts': `
					import { flushSync, mount } from 'svelte';
					import CoverHost from './__virtual_CoverHost.svelte';

					mount(CoverHost, { target: document.getElementById('app')! });
					(window as unknown as { __flush: typeof flushSync }).__flush = flushSync;
				`,
			},
		});
		bundle.css += `
			.hidden { display: none; }
			.pointer-events-none { pointer-events: none; }
			.absolute { position: absolute; }
			.inset-0 { inset: 0; }
			.bottom-3 { bottom: 0.75rem; }
			.left-3 { left: 0.75rem; }
			.z-20 { z-index: 20; }
			@media (min-width: 40rem) {
				.sm\\:pointer-events-auto { pointer-events: auto; }
				.sm\\:relative { position: relative; }
				.sm\\:inset-auto { inset: auto; }
				.sm\\:block { display: block; }
				.sm\\:flex { display: flex; }
				.sm\\:hidden { display: none; }
				.sm\\:absolute { position: absolute; }
				.sm\\:inset-0 { inset: 0; }
				.sm\\:z-20 { z-index: 20; }
			}
			#cover-42-title { display: inline; }
			button[aria-labelledby="cover-42-title cover-42-action"] {
				width: 160px;
				height: 240px;
			}
		`;
		session = await CdpBrowserSession.launch({ width: 1280, height: 800 });
		await session.loadBundle(bundle);
	}, 30_000);

	afterAll(async () => {
		await session?.close();
	});

	it('computes "{title} view details" without "Rank", toggles aria-expanded on click, and passes pointer clicks through the Rank badge', async () => {
		const triggerSelector = 'button[aria-labelledby="cover-42-title cover-42-action"]';
		const initialAx = await session.getAxSummary(triggerSelector);
		expect(initialAx.role).toBe('button');
		expect(initialAx.name).toBe('The Left Hand of Darkness view details');
		expect(initialAx.name).not.toContain('Rank');
		expect(initialAx.expanded).toBe(false);

		const afterDirectClick = await session.evaluate<{
			ariaExpanded: string | null;
			stateText: string;
		}>(`
			(() => {
				const btn = document.querySelector(${JSON.stringify(triggerSelector)});
				btn.click();
				window.__flush();
				return {
					ariaExpanded: btn.getAttribute('aria-expanded'),
					stateText: document.getElementById('expanded-state').textContent ?? '',
				};
			})()
		`);
		expect(afterDirectClick).toEqual({
			ariaExpanded: 'true',
			stateText: 'true',
		});

		const expandedAx = await session.getAxSummary(triggerSelector);
		expect(expandedAx.expanded).toBe(true);

		// Click directly over the center of the sibling Rank badge and verify pointer-events-none lets the button receive it
		const badgeCenter = await session.evaluate<{ x: number; y: number }>(`
			(() => {
				const badge = document.querySelector('.badge');
				const rect = badge.getBoundingClientRect();
				return {
					x: Math.round(rect.left + rect.width / 2),
					y: Math.round(rect.top + rect.height / 2),
				};
			})()
		`);
		await session.mouseClickAt(badgeCenter.x, badgeCenter.y);

		const afterBadgeClick = await session.evaluate<{
			ariaExpanded: string | null;
			stateText: string;
		}>(`
			(() => {
				window.__flush();
				const btn = document.querySelector(${JSON.stringify(triggerSelector)});
				return {
					ariaExpanded: btn.getAttribute('aria-expanded'),
					stateText: document.getElementById('expanded-state').textContent ?? '',
				};
			})()
		`);
		expect(afterBadgeClick).toEqual({
			ariaExpanded: 'false',
			stateText: 'false',
		});
	});
});
