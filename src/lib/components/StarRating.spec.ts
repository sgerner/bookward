import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import StarRating from './StarRating.svelte';

describe('shared StarRating component', () => {
	it('renders five Tab-focusable star buttons without roving tabindex or clear button when unrated', () => {
		const { body } = render(StarRating, {
			props: { value: 0 },
		});

		expect(body).toContain('role="group"');
		expect(body).toContain('aria-label="Optional book rating"');
		expect(body).toContain('aria-label="1 star"');
		expect(body).toContain('aria-label="5 stars"');
		expect(body).not.toContain('tabindex="-1"');
		expect(body).not.toContain('Clear rating');
		expect((body.match(/data-active="false"/g) ?? []).length).toBe(5);
	});

	it('marks active stars up to the selected rating, sets aria-pressed on the exact rating, and shows Clear rating', () => {
		const { body } = render(StarRating, {
			props: { value: 4 },
		});

		expect((body.match(/data-active="true"/g) ?? []).length).toBe(4);
		expect((body.match(/data-active="false"/g) ?? []).length).toBe(1);
		expect(body).toContain('aria-label="4 stars" aria-pressed="true"');
		expect(body).toContain('aria-label="3 stars" aria-pressed="false"');
		expect(body).toContain('Clear rating');
	});
});
