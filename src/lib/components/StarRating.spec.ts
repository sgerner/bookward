import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import StarRating from './StarRating.svelte';

describe('shared StarRating component', () => {
	it('renders a RadioGroup of five star radios with a single tabindex="0" stop and no clear button when unrated', () => {
		const { body } = render(StarRating, {
			props: { value: 0, labelledBy: 'mark-read-rating-label' },
		});

		expect(body).toContain('role="radiogroup"');
		expect(body).toContain('aria-labelledby="mark-read-rating-label"');
		expect(body).toContain('aria-label="1 star"');
		expect(body).toContain('aria-label="5 stars"');
		expect(body.match(/role="radio"/g)).toHaveLength(5);
		expect(body.match(/aria-checked="true"/g)).toBeNull();
		expect(body.match(/tabindex="0"/g)).toHaveLength(1);
		expect(body.match(/tabindex="-1"/g)).toHaveLength(4);
		expect(body).not.toContain('Clear rating');
		expect((body.match(/data-active="false"/g) ?? []).length).toBe(5);
	});

	it('marks active star glyphs up to the selected rating, sets aria-checked on the selected radio, and shows Clear rating', () => {
		const { body } = render(StarRating, {
			props: { value: 4, labelledBy: 'mark-read-rating-label' },
		});

		expect((body.match(/data-active="true"/g) ?? []).length).toBe(4);
		expect((body.match(/data-active="false"/g) ?? []).length).toBe(1);
		expect(body).toContain('aria-label="4 stars" aria-checked="true" tabindex="0"');
		expect(body).toContain('aria-label="3 stars" aria-checked="false" tabindex="-1"');
		expect(body).toContain('Clear rating');
	});

	it('renders an ARIA radiogroup with a single tabindex="0" stop on 1 star when unrated in inline history mode', () => {
		const { body } = render(StarRating, {
			props: {
				value: null,
				label: 'Rating for Station Eleven',
				groupKey: 'history-11',
			},
		});

		expect(body).toContain('role="radiogroup"');
		expect(body).toContain('aria-labelledby="');
		expect(body).toContain('>Rating for Station Eleven</span>');
		expect(body).not.toContain('aria-pressed');
		expect(body.match(/role="radio"/g)).toHaveLength(5);
		expect(body.match(/aria-checked="true"/g)).toBeNull();
		expect(body.match(/aria-checked="false"/g)).toHaveLength(5);
		expect(body.match(/tabindex="0"/g)).toHaveLength(1);
		expect(body.match(/tabindex="-1"/g)).toHaveLength(4);
		expect(body).toContain('data-rating-star="history-11-1"');
		expect(body).toContain('aria-label="1 star" aria-checked="false" tabindex="0"');
		expect(body).not.toContain('Clear rating');
		expect(body.match(/data-active="false"/g)).toHaveLength(5);
	});

	it('checks the selected radio, fills stars up to the rating, sets roving tabindex on the checked star, and renders Clear rating outside radiogroup', () => {
		const { body } = render(StarRating, {
			props: {
				value: 4,
				label: 'Rating for Piranesi',
				groupKey: 'recent-10',
				busy: true,
			},
		});

		expect(body).toContain('aria-busy="true"');
		expect(body.match(/data-active="true"/g)).toHaveLength(4);
		expect(body.match(/data-active="false"/g)).toHaveLength(1);
		expect(body.match(/aria-checked="true"/g)).toHaveLength(1);
		expect(body).toContain('aria-label="4 stars" aria-checked="true" tabindex="0"');
		expect(body).toContain('aria-label="1 star" aria-checked="false" tabindex="-1"');
		expect(body).toContain('data-rating-clear="recent-10"');
		expect(body).toContain('Clear rating');
	});
});
