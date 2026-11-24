import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRawSnippet } from "svelte";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import BookCoverTrigger, { nextCoverExpanded } from "./BookCoverTrigger.svelte";

const coverImageSnippet = createRawSnippet(() => ({
  render: () =>
    '<img src="https://covers.example/left-hand.jpg" alt="" width="160" height="240" />',
}));

type AstNode = {
  type?: string;
  name?: string;
  attributes?: Array<{
    type: string;
    name?: string;
    value?: true | Array<{ type?: string; data?: string }>;
  }>;
  [key: string]: unknown;
};

function collectNodes(node: unknown, predicate: (entry: AstNode) => boolean): AstNode[] {
  if (!node || typeof node !== "object") return [];
  const record = node as AstNode;
  const found: AstNode[] = predicate(record) ? [record] : [];
  for (const [key, value] of Object.entries(record)) {
    if (key === "attributes") continue;
    if (Array.isArray(value)) {
      for (const child of value) found.push(...collectNodes(child, predicate));
    } else if (value && typeof value === "object") {
      found.push(...collectNodes(value, predicate));
    }
  }
  return found;
}

describe("BookCoverTrigger component and +page.svelte wiring", () => {
  it("names the cover button with the book title first and view details second while rendering Rank outside the button", () => {
    const { body } = render(BookCoverTrigger, {
      props: {
        id: "cover-42",
        title: "The Left Hand of Darkness",
        score: 94,
        hasCover: true,
        expanded: true,
        children: coverImageSnippet,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).toContain('aria-labelledby="cover-42-title cover-42-action"');
    expect(body).toContain('aria-expanded="true"');
    expect(body).toContain('alt=""');
    expect(body).toContain("pointer-events-none absolute inset-0 z-0");
    expect(body.indexOf("The Left Hand of Darkness")).toBeLessThan(
      body.indexOf("view details"),
    );

    const buttonCloseIndex = body.indexOf("</button>");
    const rankIndex = body.indexOf("Rank 94");
    expect(buttonCloseIndex).toBeGreaterThan(0);
    expect(rankIndex).toBeGreaterThan(buttonCloseIndex);
    expect(body).toContain("pointer-events-none");
  });

  it("hides the mobile backdrop when hasCover is false and round-trips aria-expanded state", () => {
    let expanded = false;

    const collapsed = render(BookCoverTrigger, {
      props: {
        id: "cover-7",
        title: "Piranesi",
        score: 88,
        hasCover: false,
        expanded,
      },
    });
    expect(collapsed.body).toContain('class="hidden aspect-[2/3]');
    expect(collapsed.body).toContain('aria-expanded="false"');

    expanded = nextCoverExpanded(expanded);
    expect(expanded).toBe(true);

    const opened = render(BookCoverTrigger, {
      props: {
        id: "cover-7",
        title: "Piranesi",
        score: 88,
        hasCover: false,
        expanded,
      },
    });
    expect(opened.body).toContain('aria-expanded="true"');

    expanded = nextCoverExpanded(expanded);
    expect(expanded).toBe(false);
  });

  it("wires BookCoverTrigger in +page.svelte with children cover image and without dead coverUrl/loading props", () => {
    const pageSource = readFileSync(
      fileURLToPath(new URL("../../routes/+page.svelte", import.meta.url)),
      "utf8",
    );
    const triggers = collectNodes(
      parse(pageSource, { modern: true }).fragment,
      (entry) => entry.type === "Component" && entry.name === "BookCoverTrigger",
    );
    expect(triggers).toHaveLength(1);

    const [trigger] = triggers;
    const attrNames = (trigger.attributes ?? []).map((attr) => attr.name);
    expect(attrNames).toEqual(
      expect.arrayContaining(["title", "score", "hasCover", "expanded", "onclick"]),
    );
    expect(attrNames).not.toContain("coverUrl");
    expect(attrNames).not.toContain("loading");

    const nestedImgs = collectNodes(
      trigger,
      (entry) => entry.type === "RegularElement" && entry.name === "img",
    );
    expect(nestedImgs).toHaveLength(1);
    const altAttr = nestedImgs[0].attributes?.find((attr) => attr.name === "alt");
    expect(altAttr).toBeDefined();
    expect(
      Array.isArray(altAttr?.value)
        ? altAttr.value.map((part) => part.data ?? "").join("")
        : "",
    ).toBe("");
  });
});
