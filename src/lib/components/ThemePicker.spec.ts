import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { THEMES } from '#lib/theme.js';
import ThemePicker from './ThemePicker.svelte';

describe('ThemePicker', () => {
	it('renders compact and icon triggers with dialog ARIA attributes when closed', () => {
		const iconPicker = render(ThemePicker, { props: { compact: false } });
		expect(iconPicker.body).toContain('aria-controls="appearance-panel"');
		expect(iconPicker.body).toContain('aria-haspopup="dialog"');
		expect(iconPicker.body).toContain('aria-expanded="false"');
		expect(iconPicker.body).not.toContain('<span>Appearance</span>');
		expect(iconPicker.body).not.toContain('<dialog');

		const compactPicker = render(ThemePicker, { props: { compact: true } });
		expect(compactPicker.body).toContain('<span>Appearance</span>');
		expect(compactPicker.body).toContain('aria-expanded="false"');
	});

	it('renders #appearance-panel as a single-scroll ModalDialog with all 24 themes including Wintry', () => {
		const opened = render(ThemePicker, { props: { compact: true, open: true } });

		expect(opened.body).toContain('<dialog');
		expect(opened.body).toContain('id="appearance-panel"');
		expect(opened.body).toContain('m-auto');
		expect(opened.body).toContain('max-h-[calc(100dvh-2rem)]');
		expect(opened.body).toContain('overflow-y-auto');
		expect(opened.body).not.toContain('45vh');
		expect(opened.body).toContain('data-modal-initial-focus');
		expect(opened.body).toContain('aria-label="Close appearance picker"');

		expect(THEMES).toHaveLength(24);
		for (const theme of THEMES) {
			expect(opened.body).toContain(`aria-label="Use ${theme.label} theme"`);
		}
		expect(opened.body).toContain('aria-label="Use Wintry theme"');
	});
});
