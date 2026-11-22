import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import LabeledSelect from "./LabeledSelect.svelte";

const optionsSnippet = createRawSnippet(() => ({
  render: () =>
    '<option value="score">Best match</option><option value="title">Title A–Z</option>',
}));

describe("server-rendered LabeledSelect", () => {
  it("leads aria-labelledby with the visible label followed by an sr-only context suffix without aria-label", () => {
    const { body } = render(LabeledSelect, {
      props: {
        id: "shortlist-sort",
        label: "Sort by",
        context: "for shortlist",
        value: "score",
        children: optionsSnippet,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).toContain(
      '<label id="shortlist-sort-label" for="shortlist-sort"',
    );
    expect(body).toContain(
      '<span id="shortlist-sort-context" class="sr-only">for shortlist</span>',
    );
    expect(body).toContain(
      'aria-labelledby="shortlist-sort-label shortlist-sort-context"',
    );
    expect(body.indexOf(">Sort by</label>")).toBeLessThan(
      body.indexOf(">for shortlist</span>"),
    );
  });

  it("binds only the visible label when no context suffix is supplied", () => {
    const { body } = render(LabeledSelect, {
      props: {
        id: "genre-filter",
        label: "Genre",
        children: optionsSnippet,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).not.toContain("genre-filter-context");
    expect(body).toContain('aria-labelledby="genre-filter-label"');
  });
});
