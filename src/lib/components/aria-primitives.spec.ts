import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRawSnippet } from 'svelte';
import { parse } from 'svelte/compiler';
import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';
import { THEMES } from '#lib/theme.js';
import ChipToggle, { nextChipPressed } from './ChipToggle.svelte';
import RadioGroup, { type RadioItemState } from './RadioGroup.svelte';
import SwitchControl, { nextSwitchChecked, toggleSwitchState } from './SwitchControl.svelte';

type AstElement = {
	type: 'RegularElement' | 'Component';
	name: string;
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
	it('renders a type="button" role="switch" with labelledby/describedby associations, default focus ring, and hidden form input only when checked', () => {
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
		expect(unchecked.body).toContain('focus-visible:outline-secondary-500');
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

	it('round-trips bound checked state and onToggle callbacks across clicks while guarding disabled switches', () => {
		let boundChecked = false;
		const onToggle = vi.fn<(nextChecked: boolean) => void>();

		const firstPass = render(SwitchControl, {
			props: {
				id: 'digest-only-new',
				name: 'onlyNew',
				checked: boundChecked,
				ariaLabelledBy: 'digest-only-new-label',
			},
		});
		expect(firstPass.body).toContain('aria-checked="false"');
		expect(firstPass.body).not.toContain('<input type="hidden"');

		toggleSwitchState({
			checked: boundChecked,
			setChecked: (next) => {
				boundChecked = next;
			},
			onToggle,
		});
		expect(boundChecked).toBe(true);
		expect(onToggle).toHaveBeenCalledTimes(1);
		expect(onToggle).toHaveBeenLastCalledWith(true);

		const secondPass = render(SwitchControl, {
			props: {
				id: 'digest-only-new',
				name: 'onlyNew',
				checked: boundChecked,
				ariaLabelledBy: 'digest-only-new-label',
			},
		});
		expect(secondPass.body).toContain('aria-checked="true"');
		expect(secondPass.body).toContain('<input type="hidden" name="onlyNew" value="on"');

		toggleSwitchState({
			checked: boundChecked,
			setChecked: (next) => {
				boundChecked = next;
			},
			onToggle,
		});
		expect(boundChecked).toBe(false);
		expect(onToggle).toHaveBeenCalledTimes(2);
		expect(onToggle).toHaveBeenLastCalledWith(false);

		toggleSwitchState({
			checked: boundChecked,
			disabled: true,
			setChecked: (next) => {
				boundChecked = next;
			},
			onToggle,
		});
		expect(boundChecked).toBe(false);
		expect(onToggle).toHaveBeenCalledTimes(2);
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

		const pageSource = readFileSync(
			fileURLToPath(new URL('../../routes/+page.svelte', import.meta.url)),
			'utf8',
		);
		expect(pageSource).toContain('<ChipToggle');
		expect(pageSource).toContain('id="mark-read-rating-label"');
		expect(pageSource).toContain('labelledBy="mark-read-rating-label"');
		expect(pageSource.match(/<SwitchControl/g)).toHaveLength(4);
		expect(pageSource).not.toContain('SwitchField');
		expect(pageSource).not.toMatch(/<input[^>]*role="switch"/);

		const pageElements = collectAstElements(parse(pageSource, { modern: true }).fragment);
		const switchControls = pageElements.filter(
			(el) => el.type === 'Component' && el.name === 'SwitchControl',
		);
		expect(switchControls).toHaveLength(4);

		const expectedSwitchIds = [
			'digest-enabled',
			'digest-only-new',
			'digest-discord',
			'digest-email',
		];
		expect(switchControls.map((sw) => getStaticAttr(sw, 'id'))).toEqual(expectedSwitchIds);

		for (const sw of switchControls) {
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
