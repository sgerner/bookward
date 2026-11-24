import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ThemePicker from './ThemePicker.svelte';

describe('ThemePicker', () => {
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
});
