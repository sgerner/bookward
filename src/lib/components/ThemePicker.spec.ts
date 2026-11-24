import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ThemePicker from './ThemePicker.svelte';

describe('ThemePicker focus and template invariants', () => {
	it('renders the appearance trigger button with dialog ARIA attributes', () => {
		const { body } = render(ThemePicker, { props: { compact: false } });
		expect(body).toContain('aria-label="Choose appearance"');
		expect(body).toContain('aria-haspopup="dialog"');
	});

	it('keeps standard mode as an anchored absolute popover and compact mode as a single-scroller top-layer panel', () => {
		const standard = render(ThemePicker, { props: { compact: false } });
		expect(standard.body).toContain('aria-label="Choose appearance"');
		expect(standard.body).toContain('absolute right-0 top-[calc(100%+0.75rem)] z-50');
		expect(standard.body).not.toContain('<dialog');

		const compact = render(ThemePicker, { props: { compact: true } });
		expect(compact.body).toContain('Appearance');
		expect(compact.body).toContain('fixed right-4 top-4 left-auto bottom-auto z-[55]');
		expect(compact.body).not.toContain('max-h-[min(22rem,45vh)]');
	});

	it('uses offset two-color focus rings on mode and palette radios without 0px primary-500 outlines', () => {
		const source = readFileSync(
			join(process.cwd(), 'src/lib/components/ThemePicker.svelte'),
			'utf8',
		);
		expect(source).not.toContain('focus-visible:outline-primary-500');
		expect(source).toContain('theme-picker-option');
		expect(source).toContain('outline-offset: 2px');
		expect(source).toContain('var(--focus-ring-inner)');
		expect(source).toContain('var(--focus-ring-swatch-inner)');
		expect(source).toContain('var(--focus-ring-outer)');
	});

	it('replaces all six inline switches and star buttons in +page.svelte with SwitchControl and StarRating', () => {
		const pageSource = readFileSync(
			join(process.cwd(), 'src/routes/+page.svelte'),
			'utf8',
		);
		expect(pageSource).not.toContain('role="switch"');
		expect(pageSource).not.toContain('text-amber-300');
		expect((pageSource.match(/<SwitchControl\b/g) ?? []).length).toBe(6);
		expect((pageSource.match(/<StarRating\b/g) ?? []).length).toBe(1);
	});
});
