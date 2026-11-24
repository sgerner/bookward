import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Snippet } from "svelte";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import { describe, expect, it, vi } from "vitest";
import LabeledSelect, { applySelectValueChange } from "./LabeledSelect.svelte";

type SsrOptionRenderer = {
  option: (
    attrs: { value: string },
    body: (renderer: SsrOptionRenderer) => void,
  ) => void;
  push: (chunk: string) => void;
};

const optionsSnippet = ((renderer: SsrOptionRenderer) => {
  for (const [value, text] of [
    ["score", "Best match"],
    ["title", "Title A–Z"],
    ["author", "Author A–Z"],
  ] as const) {
    renderer.option({ value }, (inner) => {
      inner.push(text);
    });
  }
}) as unknown as Snippet;

type SelectComponentNode = {
  attributes: Array<{
    type: string;
    name?: string;
    expression?: { type?: string; name?: string };
    value?: true | Array<{ type?: string; data?: string }>;
  }>;
};

function collectLabeledSelects(node: unknown): SelectComponentNode[] {
  if (!node || typeof node !== "object") return [];
  const record = node as Record<string, unknown>;
  const found: SelectComponentNode[] = [];
  if (record.type === "Component" && record.name === "LabeledSelect") {
    found.push({
      attributes: (record.attributes as SelectComponentNode["attributes"]) ?? [],
    });
  }
  for (const [key, value] of Object.entries(record)) {
    if (key === "attributes") continue;
    if (Array.isArray(value)) {
      for (const child of value) found.push(...collectLabeledSelects(child));
    } else if (value && typeof value === "object") {
      found.push(...collectLabeledSelects(value));
    }
  }
  return found;
}

describe("LabeledSelect component and +page.svelte wiring", () => {
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

  it("round-trips bound select values and forwards onchange events", () => {
    let shortlistSort: "score" | "title" | "author" = "score";
    const onChange = vi.fn();

    const initial = render(LabeledSelect, {
      props: {
        id: "shortlist-sort",
        label: "Sort by",
        context: "for shortlist",
        value: shortlistSort,
        children: optionsSnippet,
      },
    });
    expect(initial.body).toMatch(/<option value="score"[^>]*selected/);
    expect(initial.body).not.toMatch(/<option value="author"[^>]*selected/);

    const syntheticEvent = {
      currentTarget: { value: "author" },
    } as unknown as Parameters<typeof applySelectValueChange<"score" | "title" | "author">>[0]["event"];

    const next = applySelectValueChange<"score" | "title" | "author">({
      event: syntheticEvent,
      setValue: (updated) => {
        shortlistSort = updated;
      },
      onChange,
    });
    expect(next).toBe("author");
    expect(shortlistSort).toBe("author");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(syntheticEvent);

    const updated = render(LabeledSelect, {
      props: {
        id: "shortlist-sort",
        label: "Sort by",
        context: "for shortlist",
        value: shortlistSort,
        children: optionsSnippet,
      },
    });
    expect(updated.body).toMatch(/<option value="author"[^>]*selected/);
    expect(updated.body).not.toMatch(/<option value="score"[^>]*selected/);
  });

  it("wires all five filter and sort selects in +page.svelte via bind:value without aria-label overrides", () => {
    const pageSource = readFileSync(
      fileURLToPath(new URL("../../routes/+page.svelte", import.meta.url)),
      "utf8",
    );
    const selects = collectLabeledSelects(parse(pageSource, { modern: true }).fragment);
    expect(selects).toHaveLength(5);

    const wiredBindings = selects.map((select) => {
      expect(
        select.attributes.some(
          (attr) =>
            attr.type === "Attribute" &&
            (attr.name === "aria-label" || attr.name === "aria-labelledby"),
        ),
      ).toBe(false);

      const labelAttr = select.attributes.find(
        (attr) => attr.type === "Attribute" && attr.name === "label",
      );
      const contextAttr = select.attributes.find(
        (attr) => attr.type === "Attribute" && attr.name === "context",
      );
      const bindAttr = select.attributes.find(
        (attr) => attr.type === "BindDirective" && attr.name === "value",
      );

      expect(contextAttr, "LabeledSelect in +page.svelte should define context").toBeDefined();
      expect(bindAttr, "LabeledSelect in +page.svelte should bind:value").toBeDefined();

      const label = Array.isArray(labelAttr?.value)
        ? labelAttr.value.map((part) => part.data ?? "").join("")
        : "";
      return {
        label,
        boundState: bindAttr?.expression?.name ?? "",
      };
    });

    expect(wiredBindings).toEqual([
      { label: "Genre", boundState: "genreFilter" },
      { label: "Sort by", boundState: "shortlistSort" },
      { label: "Rating", boundState: "readingHistoryRatingFilter" },
      { label: "Source", boundState: "readingHistorySourceFilter" },
      { label: "Sort by", boundState: "readingHistorySort" },
    ]);
  });
});
