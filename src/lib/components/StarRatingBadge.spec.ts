import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import StarRatingBadge from "./StarRatingBadge.svelte";

describe("StarRatingBadge", () => {
  it("renders plural screen-reader rating text and hides star glyph without aria-label", () => {
    const { body } = render(StarRatingBadge, {
      props: { rating: 4 },
    });

    expect(body).toContain('class="badge preset-tonal-secondary"');
    expect(body).toContain('<span aria-hidden="true">4 ★</span>');
    expect(body).toContain('<span class="sr-only">Your rating: 4 stars</span>');
    expect(body).not.toContain("aria-label");
  });

  it("renders singular screen-reader text for a 1-star rating and supports custom classes", () => {
    const { body } = render(StarRatingBadge, {
      props: {
        rating: 1,
        class: "shrink-0 text-xs font-semibold text-primary-600-400",
      },
    });

    expect(body).toContain('class="shrink-0 text-xs font-semibold text-primary-600-400"');
    expect(body).toContain('<span aria-hidden="true">1 ★</span>');
    expect(body).toContain('<span class="sr-only">Your rating: 1 star</span>');
    expect(body).not.toContain("aria-label");
  });

  it("omits markup when rating is null or zero", () => {
    expect(render(StarRatingBadge, { props: { rating: null } }).body).not.toContain("★");
    expect(render(StarRatingBadge, { props: { rating: 0 } }).body).not.toContain("★");
  });
});
