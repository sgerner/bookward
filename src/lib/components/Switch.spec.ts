import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import Switch from './Switch.svelte';

describe('shared Switch component', () => {
	it('renders a native submit switch button with ARIA state and busy semantics', () => {
		const { body } = render(Switch, {
			props: {
				kind: 'submit',
				checked: true,
				ariaLabel: 'Toggle Bookward demo books',
				disabled: true,
				busy: true,
			},
		});

		expect(body).toContain('<button');
		expect(body).toContain('type="submit"');
		expect(body).toContain('role="switch"');
		expect(body).toContain('aria-label="Toggle Bookward demo books"');
		expect(body).toContain('aria-checked="true"');
		expect(body).toContain('aria-busy="true"');
		expect(body).toContain('disabled');
		expect(body).toContain('data-checked="true"');
		expect(body).toContain('data-tone="primary"');
	});

	it('renders a native checkbox switch input for progressive form submission', () => {
		const { body } = render(Switch, {
			props: {
				id: 'digest-email',
				name: 'channels',
				value: 'email',
				tone: 'tertiary',
				checked: false,
				ariaLabel: 'Send digest by email',
			},
		});

		expect(body).toContain('<input');
		expect(body).toContain('type="checkbox"');
		expect(body).toContain('id="digest-email"');
		expect(body).toContain('name="channels"');
		expect(body).toContain('value="email"');
		expect(body).toContain('role="switch"');
		expect(body).toContain('aria-label="Send digest by email"');
		expect(body).toContain('aria-checked="false"');
		expect(body).toContain('data-tone="tertiary"');
		expect(body).toContain('switch-thumb-idle-dot');
	});
});
