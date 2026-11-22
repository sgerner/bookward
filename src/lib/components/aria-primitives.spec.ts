import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { THEMES } from '#lib/theme.js';
import ChipToggle, { nextChipPressed } from './ChipToggle.svelte';
import RadioGroup, { type RadioItemState } from './RadioGroup.svelte';
import SwitchControl, { nextSwitchChecked } from './SwitchControl.svelte';

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
	it('renders a type="button" role="switch" with hidden form input only when checked', () => {
		const unchecked = render(SwitchControl, {
			props: {
				id: 'digest-enabled',
				name: 'enabled',
				checked: false,
				ariaLabel: 'Enable weekly digest',
			},
		});
		expect(unchecked.body).toContain('id="digest-enabled"');
		expect(unchecked.body).toContain('type="button"');
		expect(unchecked.body).toContain('role="switch"');
		expect(unchecked.body).toContain('aria-checked="false"');
		expect(unchecked.body).not.toContain('<input');

		const checked = render(SwitchControl, {
			props: {
				id: 'digest-discord',
				name: 'channels',
				value: 'discord',
				checked: true,
				ariaLabel: 'Send digest to Discord',
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
				ariaLabel: 'Send digest by email',
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
	it('replaces stuck aria-pressed grids and sr-only checkbox switches with shared primitives', () => {
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
		expect(pageSource).not.toMatch(/<input[^>]*role="switch"/);
	});
});
