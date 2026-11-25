import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRawSnippet } from 'svelte';
import { parse } from 'svelte/compiler';
import { render } from 'svelte/server';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { THEMES } from '#lib/theme.js';
import ChipToggle, { nextChipPressed } from './ChipToggle.svelte';
import RadioGroup, { type RadioItemState } from './RadioGroup.svelte';
import SwitchControl, { nextSwitchChecked } from './SwitchControl.svelte';
import { CdpBrowserSession, compileSvelteClientBundle } from './svelte-dom-test-harness';

type AstElement = {
	type: 'RegularElement' | 'Component';
	name: string;
	start: number;
	end: number;
	attributes: Array<{
		type: string;
		name?: string;
		value?: true | Array<{ type?: string; data?: string }> | { type?: string };
	}>;
};

function collectAstElements(node: unknown): AstElement[] {
	if (!node || typeof node !== 'object') return [];
	const record = node as Record<string, unknown>;
	const found: AstElement[] = [];
	if (
		(record.type === 'RegularElement' || record.type === 'Component') &&
		typeof record.name === 'string'
	) {
		found.push({
			type: record.type,
			name: record.name,
			start: (record.start as number) ?? 0,
			end: (record.end as number) ?? 0,
			attributes: (record.attributes as AstElement['attributes']) ?? [],
		});
	}
	for (const [key, value] of Object.entries(record)) {
		if (key === 'attributes') continue;
		if (Array.isArray(value)) {
			for (const child of value) found.push(...collectAstElements(child));
		} else if (value && typeof value === 'object') {
			found.push(...collectAstElements(value));
		}
	}
	return found;
}

function getStaticAttr(el: AstElement, name: string): string | undefined {
	const attr = el.attributes.find((item) => item.type === 'Attribute' && item.name === name);
	if (!attr) return undefined;
	if (attr.value === true || !Array.isArray(attr.value)) return '';
	return attr.value.map((part) => part.data ?? '').join('');
}

describe('ChipToggle primitive', () => {
	it('renders inactive and active aria-pressed states with matching Skeleton fill classes', () => {
		const inactive = render(ChipToggle, {
			props: {
				pressed: false,
				label: 'Science Fiction',
				ariaLabel: 'Science Fiction filter',
			},
		});
		expect(inactive.body).toContain('type="button"');
		expect(inactive.body).toContain('aria-label="Science Fiction filter"');
		expect(inactive.body).toContain('aria-pressed="false"');
		expect(inactive.body).toContain('preset-tonal-secondary');
		expect(inactive.body).not.toContain('preset-filled-secondary-500');

		const active = render(ChipToggle, {
			props: {
				pressed: true,
				label: 'Science Fiction',
				ariaLabel: 'Science Fiction filter',
			},
		});
		expect(active.body).toContain('aria-pressed="true"');
		expect(active.body).toContain('preset-filled-secondary-500');
		expect(active.body).not.toContain('preset-tonal-secondary');

		expect(nextChipPressed(false)).toBe(true);
		expect(nextChipPressed(true)).toBe(false);
	});
});

describe('SwitchControl primitive', () => {
	it('renders a type="button" role="switch" with labelledby/describedby associations and hidden form input only when checked', () => {
		const switchSource = readFileSync(
			fileURLToPath(new URL('./SwitchControl.svelte', import.meta.url)),
			'utf8',
		);
		expect(switchSource).toContain('.switch-control.switch-control:not(:disabled):focus-visible');
		expect(switchSource).toContain('var(--focus-ring-outer)');

		const unchecked = render(SwitchControl, {
			props: {
				id: 'digest-enabled',
				name: 'enabled',
				checked: false,
				ariaLabelledBy: 'digest-enabled-label',
				ariaDescribedBy: 'digest-enabled-description',
			},
		});
		expect(unchecked.body).toContain('id="digest-enabled"');
		expect(unchecked.body).toContain('type="button"');
		expect(unchecked.body).toContain('role="switch"');
		expect(unchecked.body).toContain('aria-checked="false"');
		expect(unchecked.body).not.toContain('aria-label=');
		expect(unchecked.body).toContain('aria-labelledby="digest-enabled-label"');
		expect(unchecked.body).toContain('aria-describedby="digest-enabled-description"');
		expect(unchecked.body).toContain('switch-control');
		expect(unchecked.body).not.toContain('<input');

		const checked = render(SwitchControl, {
			props: {
				id: 'digest-discord',
				name: 'channels',
				value: 'discord',
				checked: true,
				ariaLabelledBy: 'digest-discord-label',
				ariaDescribedBy: 'digest-discord-description',
			},
		});
		expect(checked.body).toContain('type="button"');
		expect(checked.body).toContain('role="switch"');
		expect(checked.body).toContain('aria-checked="true"');
		expect(checked.body).toContain('preset-filled-secondary-500');
		expect(checked.body).toContain('<input type="hidden" name="channels" value="discord"');

		const tertiary = render(SwitchControl, {
			props: {
				id: 'digest-email',
				name: 'channels',
				value: 'email',
				checked: true,
				ariaLabelledBy: 'digest-email-label',
				ariaDescribedBy: 'digest-email-description',
				activeClass: 'preset-filled-tertiary-500',
			},
		});
		expect(tertiary.body).toContain('preset-filled-tertiary-500');
		expect(tertiary.body).toContain('<input type="hidden" name="channels" value="email"');

		const disabled = render(SwitchControl, {
			props: {
				name: 'enabled',
				checked: true,
				disabled: true,
				ariaLabel: 'Enable weekly digest',
			},
		});
		expect(disabled.body).toContain('aria-checked="true"');
		expect(disabled.body).toContain('<input type="hidden" name="enabled" value="on" disabled');

		expect(nextSwitchChecked(false)).toBe(true);
		expect(nextSwitchChecked(true)).toBe(false);
	});
});

describe('SwitchControl real Svelte 5 client DOM interactions', () => {
	let session: CdpBrowserSession;

	beforeAll(async () => {
		const bundle = await compileSvelteClientBundle({
			entryFile: 'src/lib/components/__virtual_switch_entry.ts',
			files: {
				'src/lib/components/__virtual_SwitchHost.svelte': `
					<script lang="ts">
						import SwitchControl from './SwitchControl.svelte';

						let directChecked = $state(false);
						let derivedOverride = $state<boolean | null>(null);
						const derivedChecked = $derived(derivedOverride ?? false);
						let disabledChecked = $state(false);
						let toggleEvents = $state<boolean[]>([]);
					</script>

					<div id="discord-row">
						<label id="digest-discord-label" for="digest-discord">Discord</label>
						<p id="digest-discord-description">Post to a webhook</p>
						<SwitchControl
							id="digest-discord"
							name="channels"
							value="discord"
							ariaLabelledBy="digest-discord-label"
							ariaDescribedBy="digest-discord-description"
							bind:checked={directChecked}
							onToggle={(next) => toggleEvents.push(next)}
						/>
						<output id="direct-state">{String(directChecked)}:{toggleEvents.join(',')}</output>
					</div>

					<div id="fn-row">
						<label id="digest-only-new-label" for="digest-only-new">Only send new books</label>
						<p id="digest-only-new-description">Skip recommendations already included in a digest.</p>
						<SwitchControl
							id="digest-only-new"
							name="onlyNew"
							ariaLabelledBy="digest-only-new-label"
							ariaDescribedBy="digest-only-new-description"
							bind:checked={() => derivedChecked, (next) => (derivedOverride = next)}
						/>
						<output id="fn-state">{String(derivedChecked)}</output>
					</div>

					<div id="disabled-row">
						<label id="disabled-label" for="disabled-switch">Locked switch</label>
						<SwitchControl
							id="disabled-switch"
							name="locked"
							disabled={true}
							ariaLabelledBy="disabled-label"
							bind:checked={disabledChecked}
							onToggle={(next) => toggleEvents.push(next)}
						/>
						<output id="disabled-state">{String(disabledChecked)}</output>
					</div>
				`,
				'src/lib/components/__virtual_switch_entry.ts': `
					import { flushSync, mount } from 'svelte';
					import SwitchHost from './__virtual_SwitchHost.svelte';

					mount(SwitchHost, { target: document.getElementById('app')! });
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

	it('toggles aria-checked, hidden FormData inputs, bound checked state, and AXTree properties via button and label clicks', async () => {
		const initialAx = await session.getAxSummary('#digest-discord');
		expect(initialAx).toEqual({
			role: 'switch',
			name: 'Discord',
			description: 'Post to a webhook',
			checked: 'false',
		});

		const afterButtonClick = await session.evaluate<{
			ariaChecked: string | null;
			hasHiddenInput: boolean;
			output: string;
		}>(`
			(() => {
				const btn = document.getElementById('digest-discord');
				btn.click();
				window.__flush();
				const hidden = document.querySelector('#discord-row input[type="hidden"][name="channels"][value="discord"]');
				return {
					ariaChecked: btn.getAttribute('aria-checked'),
					hasHiddenInput: Boolean(hidden),
					output: document.getElementById('direct-state').textContent ?? '',
				};
			})()
		`);
		expect(afterButtonClick).toEqual({
			ariaChecked: 'true',
			hasHiddenInput: true,
			output: 'true:true',
		});

		const checkedAx = await session.getAxSummary('#digest-discord');
		expect(checkedAx.checked).toBe('true');

		// Clicking the associated <label for="digest-discord"> toggles back to false and removes hidden input
		const afterLabelClick = await session.evaluate<{
			ariaChecked: string | null;
			hasHiddenInput: boolean;
			output: string;
		}>(`
			(() => {
				const label = document.getElementById('digest-discord-label');
				label.click();
				window.__flush();
				const btn = document.getElementById('digest-discord');
				const hidden = document.querySelector('#discord-row input[type="hidden"]');
				return {
					ariaChecked: btn.getAttribute('aria-checked'),
					hasHiddenInput: Boolean(hidden),
					output: document.getElementById('direct-state').textContent ?? '',
				};
			})()
		`);
		expect(afterLabelClick).toEqual({
			ariaChecked: 'false',
			hasHiddenInput: false,
			output: 'false:true,false',
		});

		// Verify Svelte 5 getter/setter bind:checked={() => ..., (next) => ...} round-trips on label + button click
		const fnRoundTrip = await session.evaluate<{
			afterFirst: string;
			afterSecond: string;
		}>(`
			(() => {
				const label = document.getElementById('digest-only-new-label');
				const btn = document.getElementById('digest-only-new');
				label.click();
				window.__flush();
				const afterFirst = \`\${btn.getAttribute('aria-checked')}:\${document.getElementById('fn-state').textContent}\`;
				btn.click();
				window.__flush();
				const afterSecond = \`\${btn.getAttribute('aria-checked')}:\${document.getElementById('fn-state').textContent}\`;
				return { afterFirst, afterSecond };
			})()
		`);
		expect(fnRoundTrip).toEqual({
			afterFirst: 'true:true',
			afterSecond: 'false:false',
		});

		// Verify disabled switch ignores both button and label clicks
		const disabledResult = await session.evaluate<{
			ariaChecked: string | null;
			stateText: string;
			toggleLog: string;
		}>(`
			(() => {
				document.getElementById('disabled-switch').click();
				document.getElementById('disabled-label').click();
				window.__flush();
				return {
					ariaChecked: document.getElementById('disabled-switch').getAttribute('aria-checked'),
					stateText: document.getElementById('disabled-state').textContent ?? '',
					toggleLog: document.getElementById('direct-state').textContent ?? '',
				};
			})()
		`);
		expect(disabledResult).toEqual({
			ariaChecked: 'false',
			stateText: 'false',
			toggleLog: 'false:true,false',
		});
	});
});

describe('RadioGroup primitive', () => {
	const radioButtonSnippet = createRawSnippet<[unknown, RadioItemState, number]>(
		(entry, radio) => ({
			render: () =>
				`<button type="button" role="radio" aria-checked="${radio().checked}" tabindex="${radio().tabIndex}">${String(entry())}</button>`,
		}),
	);

	it('exposes a single tabindex="0" stop across all 24 Skeleton theme palettes', () => {
		const { body } = render(RadioGroup, {
			props: {
				items: THEMES.map((theme) => theme.id),
				value: 'cerberus',
				labelledBy: 'appearance-theme-legend',
				columns: 2,
				item: radioButtonSnippet,
			},
		});

		expect(body).toContain('role="radiogroup"');
		expect(body).toContain('aria-labelledby="appearance-theme-legend"');
		expect(body.match(/role="radio"/g)).toHaveLength(24);
		expect(body.match(/aria-checked="true"/g)).toHaveLength(1);
		expect(body.match(/aria-checked="false"/g)).toHaveLength(23);
		expect(body.match(/tabindex="0"/g)).toHaveLength(1);
		expect(body.match(/tabindex="-1"/g)).toHaveLength(23);
		expect(body).toContain('aria-checked="true" tabindex="0">cerberus</button>');
	});

	it('places the single tabindex="0" stop on the first radio when an optional rating is cleared', () => {
		const unrated = render(RadioGroup, {
			props: {
				items: [1, 2, 3, 4, 5],
				value: null,
				labelledBy: 'mark-read-rating-label',
				columns: 1,
				selectOnMove: true,
				clearable: true,
				item: radioButtonSnippet,
			},
		});

		expect(unrated.body).toContain('aria-labelledby="mark-read-rating-label"');
		expect(unrated.body.match(/aria-checked="true"/g)).toBeNull();
		expect(unrated.body.match(/aria-checked="false"/g)).toHaveLength(5);
		expect(unrated.body.match(/tabindex="0"/g)).toHaveLength(1);
		expect(unrated.body.match(/tabindex="-1"/g)).toHaveLength(4);
		expect(unrated.body).toContain('aria-checked="false" tabindex="0">1</button>');
	});
});

describe('ThemePicker and +page.svelte ARIA wiring', () => {
	it('replaces stuck aria-pressed grids and sr-only checkbox switches with shared primitives and label/description bindings', () => {
		const themePickerSource = readFileSync(
			fileURLToPath(new URL('./ThemePicker.svelte', import.meta.url)),
			'utf8',
		);
		expect(themePickerSource).not.toContain('aria-pressed');
		expect(themePickerSource).toContain('id="appearance-color-mode-legend"');
		expect(themePickerSource).toContain('labelledBy="appearance-color-mode-legend"');
		expect(themePickerSource).toContain('id="appearance-theme-legend"');
		expect(themePickerSource).toContain('labelledBy="appearance-theme-legend"');
		expect(themePickerSource.match(/<RadioGroup/g)).toHaveLength(2);

		const switchControlAst = collectAstElements(
			parse(
				readFileSync(fileURLToPath(new URL('./SwitchControl.svelte', import.meta.url)), 'utf8'),
				{ modern: true },
			).fragment,
		);
		expect(
			switchControlAst.filter(
				(el) =>
					el.type === 'RegularElement' &&
					el.name === 'button' &&
					getStaticAttr(el, 'role') === 'switch',
			),
		).toHaveLength(1);

		const pageSource = readFileSync(
			fileURLToPath(new URL('../../routes/+page.svelte', import.meta.url)),
			'utf8',
		);
		expect(pageSource).toContain('<ChipToggle');
		expect(pageSource).toContain('id="mark-read-rating-label"');
		expect(pageSource).toContain('labelledBy="mark-read-rating-label"');
		expect(pageSource.match(/<SwitchControl\b/g)).toHaveLength(6);
		expect(pageSource).not.toContain('SwitchField');
		expect(pageSource).not.toMatch(/<input[^>]*role="switch"/);

		const pageElements = collectAstElements(parse(pageSource, { modern: true }).fragment);
		const stickyShell = pageElements.find(
			(el) => getStaticAttr(el, 'data-sticky-shell') !== undefined,
		);
		expect(stickyShell).toBeDefined();

		const switchControls = pageElements.filter(
			(el) => el.type === 'Component' && el.name === 'SwitchControl',
		);
		expect(switchControls).toHaveLength(6);
		for (const sw of switchControls) {
			expect(sw.start).toBeGreaterThanOrEqual(stickyShell!.start);
			expect(sw.end).toBeLessThanOrEqual(stickyShell!.end);
		}

		const digestSwitches = switchControls.filter((sw) =>
			(getStaticAttr(sw, 'id') ?? '').startsWith('digest-'),
		);
		expect(digestSwitches.map((sw) => getStaticAttr(sw, 'id'))).toEqual([
			'digest-enabled',
			'digest-only-new',
			'digest-discord',
			'digest-email',
		]);

		for (const sw of digestSwitches) {
			const id = getStaticAttr(sw, 'id')!;
			expect(getStaticAttr(sw, 'ariaLabel')).toBeUndefined();
			expect(getStaticAttr(sw, 'ariaLabelledBy')).toBe(`${id}-label`);
			expect(getStaticAttr(sw, 'ariaDescribedBy')).toBe(`${id}-description`);
			expect(
				sw.attributes.some((attr) => attr.type === 'BindDirective' && attr.name === 'checked'),
			).toBe(true);

			const matchingLabel = pageElements.find(
				(el) =>
					el.type === 'RegularElement' &&
					el.name === 'label' &&
					getStaticAttr(el, 'id') === `${id}-label` &&
					getStaticAttr(el, 'for') === id,
			);
			expect(matchingLabel, `missing <label id="${id}-label" for="${id}">`).toBeDefined();

			const matchingDescription = pageElements.find(
				(el) =>
					el.type === 'RegularElement' &&
					el.name === 'p' &&
					getStaticAttr(el, 'id') === `${id}-description`,
			);
			expect(matchingDescription, `missing <p id="${id}-description">`).toBeDefined();
		}
	});
});
