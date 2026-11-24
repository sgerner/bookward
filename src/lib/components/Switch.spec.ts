import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import SwitchControl from './SwitchControl.svelte';

const DISABLED_BUTTON_ATTR = /<button\b[^>]*\sdisabled(?:=""|(?=[\s>]))/;

describe('SwitchControl contrast and submit semantics', () => {
	it('renders a native submit switch button with ARIA state, busy, and strict disabled attributes', () => {
		const disabledSubmit = render(SwitchControl, {
			props: {
				type: 'submit',
				tone: 'primary',
				checked: true,
				ariaLabel: 'Toggle Bookward demo books',
				disabled: true,
				busy: true,
			},
		});

		expect(disabledSubmit.body).toContain('<button');
		expect(disabledSubmit.body).toContain('type="submit"');
		expect(disabledSubmit.body).toContain('role="switch"');
		expect(disabledSubmit.body).toContain('aria-label="Toggle Bookward demo books"');
		expect(disabledSubmit.body).toContain('aria-checked="true"');
		expect(disabledSubmit.body).toContain('aria-busy="true"');
		expect(disabledSubmit.body).toMatch(DISABLED_BUTTON_ATTR);
		expect(disabledSubmit.body).toContain('data-checked="true"');
		expect(disabledSubmit.body).toContain('data-tone="primary"');
		expect(disabledSubmit.body).not.toContain('<input');

		const enabledSubmit = render(SwitchControl, {
			props: {
				type: 'submit',
				tone: 'primary',
				checked: false,
				ariaLabel: 'Toggle Bookward demo books',
				disabled: false,
				busy: false,
			},
		});
		expect(enabledSubmit.body).not.toMatch(DISABLED_BUTTON_ATTR);
		expect(enabledSubmit.body).toContain('aria-busy="false"');
		expect(enabledSubmit.body).toContain('switch-thumb-idle-dot');
	});

	it('infers contrast tone from activeClass when tone prop is omitted', () => {
		const tertiary = render(SwitchControl, {
			props: {
				id: 'digest-email',
				name: 'channels',
				value: 'email',
				checked: false,
				activeClass: 'preset-filled-tertiary-500',
				ariaLabel: 'Send digest by email',
			},
		});

		expect(tertiary.body).toContain('type="button"');
		expect(tertiary.body).toContain('id="digest-email"');
		expect(tertiary.body).toContain('role="switch"');
		expect(tertiary.body).toContain('aria-checked="false"');
		expect(tertiary.body).toContain('data-tone="tertiary"');
		expect(tertiary.body).toContain('switch-thumb-idle-dot');
	});
});
