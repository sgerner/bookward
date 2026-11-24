import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Snippet } from 'svelte';
import { parse } from 'svelte/compiler';
import { render } from 'svelte/server';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import LabeledSelect from './LabeledSelect.svelte';
import { CdpBrowserSession, compileSvelteClientBundle } from './svelte-dom-test-harness';

type SsrOptionRenderer = {
	option: (
		attrs: { value: string },
		body: (renderer: SsrOptionRenderer) => void,
	) => void;
	push: (chunk: string) => void;
};

const optionsSnippet = ((renderer: SsrOptionRenderer) => {
	for (const [value, text] of [
		['score', 'Best match'],
		['title', 'Title A–Z'],
		['author', 'Author A–Z'],
	] as const) {
		renderer.option({ value }, (inner) => {
			inner.push(text);
		});
	}
}) as unknown as Snippet;

type SelectComponentNode = {
	start: number;
	end: number;
	attributes: Array<{
		type: string;
		name?: string;
		start?: number;
		end?: number;
		expression?: { type?: string; name?: string };
		value?: true | Array<{ type?: string; data?: string }>;
	}>;
};

function collectLabeledSelects(node: unknown): SelectComponentNode[] {
	if (!node || typeof node !== 'object') return [];
	const record = node as Record<string, unknown>;
	const found: SelectComponentNode[] = [];
	if (record.type === 'Component' && record.name === 'LabeledSelect') {
		found.push({
			start: record.start as number,
			end: record.end as number,
			attributes: (record.attributes as SelectComponentNode['attributes']) ?? [],
		});
	}
	for (const [key, value] of Object.entries(record)) {
		if (key === 'attributes') continue;
		if (Array.isArray(value)) {
			for (const child of value) found.push(...collectLabeledSelects(child));
		} else if (value && typeof value === 'object') {
			found.push(...collectLabeledSelects(value));
		}
	}
	return found;
}

describe('LabeledSelect component and +page.svelte AST contract', () => {
	it('pairs a native <label for> containing the visible label and optional sr-only suffix without aria-label overrides', () => {
		const { body } = render(LabeledSelect, {
			props: {
				id: 'shortlist-sort',
				label: 'Sort by',
				srLabel: 'for shortlist',
				value: 'score',
				describedBy: ['sort-hint', 'sort-hint', 'sort-extra'],
				children: optionsSnippet,
			},
		});

		expect(body).not.toContain('aria-label=');
		expect(body).not.toContain('aria-labelledby=');
		expect(body).toContain('<label for="shortlist-sort" class="block">Sort by');
		expect(body).toContain('<span class="sr-only"> for shortlist</span>');
		expect(body.indexOf('Sort by')).toBeLessThan(body.indexOf(' for shortlist</span>'));
		expect(body).toContain('id="shortlist-sort"');
		expect(body).toContain('aria-describedby="sort-hint sort-extra"');
	});

	it('renders only the visible label text when srLabel is omitted or blank', () => {
		const { body } = render(LabeledSelect, {
			props: {
				id: 'genre-filter',
				label: 'Genre',
				srLabel: '   ',
				children: optionsSnippet,
			},
		});

		expect(body).not.toContain('aria-label=');
		expect(body).not.toContain('sr-only');
		expect(body).not.toContain('aria-describedby=');
		expect(body).toContain('<label for="genre-filter" class="block">Genre');
	});

	it('wires all five filter and sort selects in +page.svelte via srLabel and bind:value without aria-label overrides', () => {
		const pageSource = readFileSync(
			fileURLToPath(new URL('../../routes/+page.svelte', import.meta.url)),
			'utf8',
		);
		const selects = collectLabeledSelects(parse(pageSource, { modern: true }).fragment);
		expect(selects).toHaveLength(5);

		const wiredBindings = selects.map((select) => {
			expect(
				select.attributes.some(
					(attr) =>
						attr.type === 'Attribute' &&
						(attr.name === 'aria-label' || attr.name === 'aria-labelledby'),
				),
			).toBe(false);

			const labelAttr = select.attributes.find(
				(attr) => attr.type === 'Attribute' && attr.name === 'label',
			);
			const srLabelAttr = select.attributes.find(
				(attr) => attr.type === 'Attribute' && attr.name === 'srLabel',
			);
			const bindAttr = select.attributes.find(
				(attr) => attr.type === 'BindDirective' && attr.name === 'value',
			);

			expect(srLabelAttr, 'LabeledSelect in +page.svelte should define srLabel').toBeDefined();
			expect(bindAttr, 'LabeledSelect in +page.svelte should bind:value').toBeDefined();

			const label = Array.isArray(labelAttr?.value)
				? labelAttr.value.map((part) => part.data ?? '').join('')
				: '';
			return {
				label,
				boundState:
					bindAttr?.expression?.name ??
					pageSource.slice(bindAttr?.start ?? 0, bindAttr?.end ?? 0),
			};
		});

		expect(wiredBindings).toEqual([
			{
				label: 'Genre',
				boundState: 'bind:value={() => viewGenre, (next) => setGenreForView(view, next)}',
			},
			{ label: 'Sort by', boundState: 'shortlistSort' },
			{ label: 'Rating', boundState: 'readingHistoryRatingFilter' },
			{ label: 'Source', boundState: 'readingHistorySourceFilter' },
			{ label: 'Sort by', boundState: 'readingHistorySort' },
		]);
	});
});

describe('LabeledSelect real Svelte 5 client DOM bindings', () => {
	let session: CdpBrowserSession;

	beforeAll(async () => {
		const bundle = await compileSvelteClientBundle({
			entryFile: 'src/lib/components/__virtual_select_entry.ts',
			files: {
				'src/lib/components/__virtual_SelectHost.svelte': `
					<script lang="ts">
						import LabeledSelect from './LabeledSelect.svelte';

						let sortValue = $state<'score' | 'title' | 'author'>('score');
						let genreByView = $state<Record<string, string>>({ discover: '' });
						let changeEvents = $state<string[]>([]);

						function setGenre(next: string) {
							genreByView.discover = next;
						}
					</script>

					<LabeledSelect
						id="shortlist-sort"
						label="Sort by"
						srLabel="for shortlist"
						bind:value={sortValue}
						onchange={(event) => changeEvents.push(event.currentTarget.value)}
					>
						<option value="score">Best match</option>
						<option value="title">Title A–Z</option>
						<option value="author">Author A–Z</option>
					</LabeledSelect>

					<LabeledSelect
						id="plain-genre"
						label="Genre"
						bind:value={() => genreByView.discover, (next) => setGenre(next)}
					>
						<option value="">All genres</option>
						<option value="Science Fiction">Science Fiction</option>
						<option value="Fantasy">Fantasy</option>
					</LabeledSelect>

					<button id="reset-sort" type="button" onclick={() => (sortValue = 'title')}>
						Set title from parent
					</button>

					<output id="select-state">{sortValue}|{genreByView.discover}|{changeEvents.join(',')}</output>
				`,
				'src/lib/components/__virtual_select_entry.ts': `
					import { flushSync, mount } from 'svelte';
					import SelectHost from './__virtual_SelectHost.svelte';

					mount(SelectHost, { target: document.getElementById('app')! });
					(window as unknown as { __flush: typeof flushSync }).__flush = flushSync;
				`,
			},
		});
		session = await CdpBrowserSession.launch();
		await session.loadBundle(bundle);
	}, 30_000);

	afterAll(async () => {
		await session?.close();
	});

	it('round-trips direct and getter/setter bind:value on DOM change events, syncs parent updates, and exposes label-first AXTree names', async () => {
		const sortAx = await session.getAxSummary('#shortlist-sort');
		expect(sortAx.name).toBe('Sort by for shortlist');

		const genreAx = await session.getAxSummary('#plain-genre');
		expect(genreAx.name).toBe('Genre');

		const afterDomChanges = await session.evaluate<{
			stateText: string;
			selectedSort: string;
			selectedGenre: string;
		}>(`
			(() => {
				const sortEl = document.getElementById('shortlist-sort');
				sortEl.value = 'author';
				sortEl.dispatchEvent(new Event('change', { bubbles: true }));

				const genreEl = document.getElementById('plain-genre');
				genreEl.value = 'Science Fiction';
				genreEl.dispatchEvent(new Event('change', { bubbles: true }));
				window.__flush();

				return {
					stateText: document.getElementById('select-state').textContent ?? '',
					selectedSort: sortEl.value,
					selectedGenre: genreEl.value,
				};
			})()
		`);
		expect(afterDomChanges).toEqual({
			stateText: 'author|Science Fiction|author',
			selectedSort: 'author',
			selectedGenre: 'Science Fiction',
		});

		const afterParentMutation = await session.evaluate<{
			stateText: string;
			selectedSort: string;
		}>(`
			(() => {
				document.getElementById('reset-sort').click();
				window.__flush();
				const sortEl = document.getElementById('shortlist-sort');
				return {
					stateText: document.getElementById('select-state').textContent ?? '',
					selectedSort: sortEl.value,
				};
			})()
		`);
		expect(afterParentMutation).toEqual({
			stateText: 'title|Science Fiction|author',
			selectedSort: 'title',
		});
	});
});

describe('+page.svelte interactive client DOM wiring across views', () => {
	let session: CdpBrowserSession;

	beforeAll(async () => {
		const bundle = await compileSvelteClientBundle({
			entryFile: 'src/lib/components/__virtual_page_entry.ts',
			files: {
				'src/lib/components/__virtual_page_entry.ts': `
					import { flushSync, mount } from 'svelte';
					import ReaderPage from '../../routes/+page.svelte';

					const discoverBooks = [
						{
							id: 101,
							title: 'The Left Hand of Darkness',
							author: 'Ursula K. Le Guin',
							synopsis: 'An envoy visits the winter world of Gethen.',
							cover_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
							source_url: 'https://books.example/left-hand',
							published_on: '1969-03-01',
							published_kind: 'published',
							genres: ['Science Fiction'],
							score: 96,
							metadata_confidence: 0.95,
							reason: 'Strong thematic overlap with speculative anthropology.',
							status: 'recommended',
							source: 'Bookward Curated',
							recommendation_run_id: 'run-1',
						},
						{
							id: 102,
							title: 'Piranesi',
							author: 'Susanna Clarke',
							synopsis: 'Infinite halls flooded by indoor tides.',
							cover_url: '',
							source_url: 'https://books.example/piranesi',
							published_on: '2020-09-15',
							published_kind: 'published',
							genres: ['Fantasy'],
							score: 91,
							metadata_confidence: 0.92,
							reason: 'Matches atmospheric literary fantasy.',
							status: 'recommended',
							source: 'Bookward Curated',
							recommendation_run_id: 'run-1',
						},
						{
							id: 201,
							title: 'Zebra Station',
							author: 'Ada Palmer',
							synopsis: 'Orbital diplomacy.',
							cover_url: '',
							source_url: '',
							published_on: '2024-01-10',
							published_kind: 'published',
							genres: ['Science Fiction'],
							score: 95,
							metadata_confidence: 0.9,
							reason: 'Top shortlist score.',
							status: 'saved',
							source: 'Bookward Curated',
							recommendation_run_id: 'run-1',
						},
						{
							id: 202,
							title: 'Ancillary Justice',
							author: 'Ann Leckie',
							synopsis: 'Starship AI in a single human body.',
							cover_url: '',
							source_url: '',
							published_on: '2013-10-01',
							published_kind: 'published',
							genres: ['Fantasy'],
							score: 84,
							metadata_confidence: 0.9,
							reason: 'Second shortlist item alphabetically first.',
							status: 'saved',
							source: 'Bookward Curated',
							recommendation_run_id: 'run-1',
						},
					];

					const readingItems = [
						{
							id: 301,
							title: 'Station Eleven',
							author: 'Emily St. John Mandel',
							rating: 5,
							read_at: '2026-05-10',
							source: 'goodreads',
							created_at: '2026-05-10T00:00:00Z',
							rank_score: 92,
							algorithm_score: 92,
							algorithm_explanation: ['High affinity'],
							metadata_confidence: 0.95,
						},
						{
							id: 302,
							title: 'A Memory Called Empire',
							author: 'Arkady Martine',
							rating: null,
							read_at: '2026-02-01',
							source: 'manual',
							created_at: '2026-02-01T00:00:00Z',
							rank_score: 85,
							algorithm_score: 85,
							algorithm_explanation: ['Imperial intrigue'],
							metadata_confidence: 0.9,
						},
					];

					window.fetch = async (input) => {
						const rawUrl = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
						const parsed = new URL(rawUrl, 'http://bookward.test');
						if (parsed.pathname === '/api/reading-history') {
							return new Response(JSON.stringify({ items: readingItems }), {
								status: 200,
								headers: { 'content-type': 'application/json' },
							});
						}
						if (parsed.pathname === '/api/reading-history/scores') {
							return new Response(JSON.stringify({ scores: [] }), {
								status: 200,
								headers: { 'content-type': 'application/json' },
							});
						}
						if (parsed.pathname === '/api/recommendations') {
							const genre = (parsed.searchParams.get('genre') ?? '').toLowerCase();
							const status = parsed.searchParams.get('status') ?? 'recommended';
							const items = discoverBooks.filter(
								(b) =>
									b.status === status &&
									(!genre || b.genres.some((g) => g.toLowerCase() === genre)),
							);
							return new Response(JSON.stringify({ items, has_more: false }), {
								status: 200,
								headers: { 'content-type': 'application/json' },
							});
						}
						return new Response(JSON.stringify({ ok: true }), {
							status: 200,
							headers: { 'content-type': 'application/json' },
						});
					};

					const data = {
						user: {
							id: 1,
							username: 'reader',
							display_name: 'Reader',
							role: 'admin',
							profile_id: 'legacy',
							must_change_password: false,
							auth_methods: ['password'],
						},
						recommendation_run_id: 'run-1',
						books: discoverBooks,
						recommendations_has_more: false,
						recommendation_genres: ['Fantasy', 'Science Fiction'],
						decisions: [],
						decisions_has_more: false,
						decision_genres: [],
						history: readingItems.map((r) => ({
							id: r.id,
							title: r.title,
							author: r.author,
							rating: r.rating,
							read_at: r.read_at,
						})),
						readCount: readingItems.length,
						backupStatus: {
							enabled: false,
							interval_hours: 0,
							retention_count: 0,
							last_success_at: null,
							last_error: null,
							next_backup_at: null,
							backups: [],
						},
						digestReview: {
							requested: false,
							ids: [],
							books: [],
						},
						sources: [],
						profile: {
							goodreads_url: '',
							goodreads_rss_last_sync_at: '',
							goodreads_rss_last_status: '',
							goodreads_rss_last_attempt_at: '',
							goodreads_rss_last_error: '',
							default_source_enabled: 1,
							librar_url: '',
							librar_connected: 0,
							embedding_backend: 'local',
							embedding_model: 'hashing-768',
							embedding_url: '',
							embedding_api_key_set: false,
							librarr_url: '',
							librarr_api_key_set: false,
							librarr_media_type: 'ebook',
							source_sync_interval_hours: 24,
							digest: {
								enabled: false,
								channels: [],
								day: 1,
								time: '08:00',
								timezone: 'UTC',
								minimum_score: 75,
								maximum_books: 5,
								only_new: true,
								app_url: 'http://bookward.test',
								discord_webhook_set: false,
								email_to: '',
								email_from: '',
								smtp_host: '',
								smtp_port: 587,
								smtp_security: 'starttls',
								smtp_username_set: false,
								smtp_password_set: false,
								last_period: '',
								last_delivery: null,
							},
							api_tokens: [],
						},
					};

					mount(ReaderPage, {
						target: document.getElementById('app')!,
						props: { data, form: null },
					});
					(window as unknown as { __flush: typeof flushSync }).__flush = flushSync;
				`,
			},
		});
		bundle.css += `
			.hidden { display: none; }
			@media (min-width: 40rem) {
				.sm\\:block { display: block; }
				.sm\\:flex { display: flex; }
				.sm\\:hidden { display: none; }
			}
		`;
		session = await CdpBrowserSession.launch({ width: 1280, height: 900 });
		await session.loadBundle(bundle);
	}, 45_000);

	afterAll(async () => {
		await session?.close();
	});

	it('wires BookCoverTrigger, LabeledSelect, and Weekly Digest SwitchControl interactions end-to-end in +page.svelte', async () => {
		// 1. Discover view: BookCoverTrigger button names card without Rank and toggles <details>
		const firstCoverBtnSelector = 'article button[aria-expanded]';
		const coverAx = await session.getAxSummary(firstCoverBtnSelector);
		expect(coverAx.name).toBe('The Left Hand of Darkness view details');
		expect(coverAx.name).not.toContain('Rank');

		const expandedReason = await session.evaluate<{
			ariaExpanded: string | null;
			detailsOpen: boolean;
			text: string;
		}>(`
			(() => {
				const article = document.querySelector('article');
				const coverBtn = article.querySelector('button[aria-expanded]');
				coverBtn.click();
				window.__flush();
				const details = article.querySelector('details');
				return {
					ariaExpanded: coverBtn.getAttribute('aria-expanded'),
					detailsOpen: details.open,
					text: details.textContent ?? '',
				};
			})()
		`);
		expect(expandedReason.ariaExpanded).toBe('true');
		expect(expandedReason.detailsOpen).toBe(true);
		expect(expandedReason.text).toContain(
			'Strong thematic overlap with speculative anthropology.',
		);

		// Discover Genre LabeledSelect filters visible book cards via server-backed getter/setter binding
		const discoverGenreAx = await session.getAxSummary('main select');
		expect(discoverGenreAx.name).toBe('Genre for books');

		const filteredTitles = await session.evaluate<string[]>(`
			(async () => {
				const genreSelect = document.querySelector('main select');
				genreSelect.value = 'Fantasy';
				genreSelect.dispatchEvent(new Event('change', { bubbles: true }));
				window.__flush();
				await new Promise((r) => setTimeout(r, 260));
				window.__flush();
				return Array.from(document.querySelectorAll('article h2')).map((h) => h.textContent?.trim() ?? '');
			})()
		`);
		expect(filteredTitles).toEqual(['Piranesi']);

		// 2. Shortlist view: LabeledSelect "Sort by for shortlist" re-orders cards
		const shortlistOrder = await session.evaluate<{
			initial: string[];
			sortedByTitle: string[];
			selectIds: string[];
		}>(`
			(() => {
				const navButtons = Array.from(document.querySelectorAll('header nav button'));
				const shortlistBtn = navButtons.find((b) => b.textContent?.includes('Shortlist'));
				shortlistBtn.click();
				window.__flush();

				const initial = Array.from(document.querySelectorAll('article h2')).map((h) => h.textContent?.trim() ?? '');
				const selects = Array.from(document.querySelectorAll('main select'));
				const sortSelect = selects[1];
				sortSelect.value = 'title';
				sortSelect.dispatchEvent(new Event('change', { bubbles: true }));
				window.__flush();
				const sortedByTitle = Array.from(document.querySelectorAll('article h2')).map((h) => h.textContent?.trim() ?? '');
				return {
					initial,
					sortedByTitle,
					selectIds: selects.map((s) => s.id),
				};
			})()
		`);
		expect(shortlistOrder.initial).toEqual(['Zebra Station', 'Ancillary Justice']);
		expect(shortlistOrder.sortedByTitle).toEqual(['Ancillary Justice', 'Zebra Station']);

		const shortlistSortAx = await session.getAxSummary(`#${shortlistOrder.selectIds[1]}`);
		expect(shortlistSortAx.name).toBe('Sort by for shortlist');

		// 3. Reading History view: Rating & Sort LabeledSelects filter and re-order read books
		const historyResult = await session.evaluate<{
			selectIds: string[];
			initialTitles: string[];
			unratedTitles: string[];
			sortedByTitle: string[];
		}>(`
			(async () => {
				const navButtons = Array.from(document.querySelectorAll('header nav button'));
				const historyBtn = navButtons.find((b) => b.textContent?.includes('History'));
				historyBtn.click();
				window.__flush();

				const readChip = Array.from(document.querySelectorAll('section[aria-label="History filters"] button')).find((b) =>
					b.textContent?.includes('Read'),
				);
				readChip.click();
				window.__flush();
				await new Promise((r) => setTimeout(r, 40));
				window.__flush();

				const selects = Array.from(document.querySelectorAll('section[aria-label="Read books"] select'));
				const readTitles = () =>
					Array.from(document.querySelectorAll('section[aria-label="Read books"] h2')).map(
						(el) => el.textContent?.trim() ?? '',
					);

				const initialTitles = readTitles();
				selects[0].value = 'unrated';
				selects[0].dispatchEvent(new Event('change', { bubbles: true }));
				window.__flush();
				const unratedTitles = readTitles();

				selects[0].value = 'all';
				selects[0].dispatchEvent(new Event('change', { bubbles: true }));
				selects[2].value = 'title';
				selects[2].dispatchEvent(new Event('change', { bubbles: true }));
				window.__flush();
				const sortedByTitle = readTitles();

				return {
					selectIds: selects.map((s) => s.id),
					initialTitles,
					unratedTitles,
					sortedByTitle,
				};
			})()
		`);
		expect(historyResult.initialTitles).toEqual(['Station Eleven', 'A Memory Called Empire']);
		expect(historyResult.unratedTitles).toEqual(['A Memory Called Empire']);
		expect(historyResult.sortedByTitle).toEqual(['A Memory Called Empire', 'Station Eleven']);

		expect((await session.getAxSummary(`#${historyResult.selectIds[0]}`)).name).toBe(
			'Rating for read books',
		);
		expect((await session.getAxSummary(`#${historyResult.selectIds[1]}`)).name).toBe(
			'Source for read books',
		);
		expect((await session.getAxSummary(`#${historyResult.selectIds[2]}`)).name).toBe(
			'Sort by for read books',
		);

		// 4. Settings -> Weekly Digest: SwitchControl label/description AXTree & live click toggle
		await session.evaluate(`
			(() => {
				const navButtons = Array.from(document.querySelectorAll('header nav button'));
				const settingsBtn = navButtons.find((b) => b.textContent?.includes('Settings'));
				settingsBtn.click();
				window.__flush();
			})()
		`);

		expect(await session.getAxSummary('#digest-enabled')).toEqual({
			role: 'switch',
			name: 'Weekly digest',
			description: 'A short list of your strongest new matches, on your schedule.',
			checked: 'false',
		});
		expect(await session.getAxSummary('#digest-only-new')).toEqual({
			role: 'switch',
			name: 'Only send new books',
			description: 'Skip recommendations already included in a digest.',
			checked: 'true',
		});
		expect(await session.getAxSummary('#digest-discord')).toEqual({
			role: 'switch',
			name: 'Discord',
			description: 'Post to a webhook',
			checked: 'false',
		});
		expect(await session.getAxSummary('#digest-email')).toEqual({
			role: 'switch',
			name: 'Email',
			description: 'Send through your SMTP server',
			checked: 'false',
		});

		const digestToggle = await session.evaluate<{
			ariaChecked: string | null;
			hasDiscordChannelInput: boolean;
			hasWebhookField: boolean;
			hasDirtyNotice: boolean;
		}>(`
			(() => {
				const discordLabel = document.getElementById('digest-discord-label');
				discordLabel.click();
				window.__flush();
				const discordSwitch = document.getElementById('digest-discord');
				const hiddenChannel = document.querySelector('input[type="hidden"][name="channels"][value="discord"]');
				const webhookField = document.querySelector('input[name="discordWebhook"][type="url"]');
				const notices = Array.from(document.querySelectorAll('[role="status"]')).map((n) => n.textContent ?? '');
				return {
					ariaChecked: discordSwitch.getAttribute('aria-checked'),
					hasDiscordChannelInput: Boolean(hiddenChannel),
					hasWebhookField: Boolean(webhookField),
					hasDirtyNotice: notices.some((t) => t.includes('Save digest settings to apply your edits')),
				};
			})()
		`);
		expect(digestToggle).toEqual({
			ariaChecked: 'true',
			hasDiscordChannelInput: true,
			hasWebhookField: true,
			hasDirtyNotice: true,
		});
	});
});
