import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "svelte/compiler";

type AstNode = {
  type: string;
  name?: string;
  start: number;
  end: number;
  attributes?: Array<{
    type: string;
    name?: string;
    value?: true | Array<{ type: string; data?: string }> | { type: string };
  }>;
  expression?: { start: number; end: number };
  fragment?: AstNode;
  consequent?: AstNode;
  alternate?: AstNode | null;
  body?: AstNode;
  nodes?: AstNode[];
};

const routeTemplates = [
  "../routes/+page.svelte",
  "../routes/login/+page.svelte",
  "../routes/+error.svelte",
];

function readTemplate(relativePath: string) {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

function collectNodes(
  node: unknown,
  predicate: (candidate: AstNode) => boolean,
): AstNode[] {
  if (!node || typeof node !== "object") return [];
  const record = node as AstNode;
  const found: AstNode[] = [];
  if (predicate(record)) found.push(record);

  for (const value of Object.values(record)) {
    if (Array.isArray(value)) {
      for (const item of value) found.push(...collectNodes(item, predicate));
    } else if (value && typeof value === "object") {
      found.push(...collectNodes(value, predicate));
    }
  }
  return found;
}

function staticAttribute(element: AstNode, attrName: string): string | null {
  const attr = element.attributes?.find(
    (candidate) => candidate.type === "Attribute" && candidate.name === attrName,
  );
  if (!attr || !Array.isArray(attr.value)) return null;
  return attr.value.map((part) => part.data ?? "").join("");
}

function hasAttribute(element: AstNode, attrName: string): boolean {
  return Boolean(
    element.attributes?.some(
      (candidate) => candidate.type === "Attribute" && candidate.name === attrName,
    ),
  );
}

describe("cover proportions, intrinsic image sizes, and modal viewports", () => {
  it("declares positive intrinsic width and height attributes on every route img", () => {
    for (const templatePath of routeTemplates) {
      const source = readTemplate(templatePath);
      const ast = parse(source, { modern: true }).fragment;
      const images = collectNodes(
        ast,
        (node) => node.type === "RegularElement" && node.name === "img",
      );

      expect(images.length, `${templatePath} should have img elements`).toBeGreaterThan(0);

      for (const image of images) {
        const snippet = source.slice(image.start, image.end);
        const width = Number(staticAttribute(image, "width"));
        const height = Number(staticAttribute(image, "height"));

        expect(Number.isInteger(width) && width > 0, `Missing width on ${snippet}`).toBe(true);
        expect(Number.isInteger(height) && height > 0, `Missing height on ${snippet}`).toBe(true);

        if (snippet.includes("bookwardMark")) {
          expect([width, height], `Expected 128x128 SVG dimensions on ${snippet}`).toEqual([
            128,
            128,
          ]);
        } else {
          expect(
            width / height,
            `Expected 2:3 intrinsic cover ratio on ${snippet}`,
          ).toBeCloseTo(2 / 3, 5);
        }
      }
    }
  });

  it("mounts a single 2:3 top-aligned cover image per recommendation card without row stretch", () => {
    const source = readTemplate("../routes/+page.svelte");
    const ast = parse(source, { modern: true }).fragment;

    const bookLoop = collectNodes(
      ast,
      (node) =>
        node.type === "EachBlock" &&
        Boolean(
          node.expression &&
            source.slice(node.expression.start, node.expression.end).trim() ===
              "viewVisibleBooks",
        ),
    )[0];
    expect(bookLoop).toBeDefined();

    const cardGridSection = collectNodes(
      ast,
      (node) =>
        node.type === "RegularElement" &&
        node.name === "section" &&
        node.start < bookLoop.start &&
        node.end >= bookLoop.end,
    ).at(-1);
    expect(cardGridSection).toBeDefined();
    expect(staticAttribute(cardGridSection!, "class")).toContain("lg:items-start");

    const cardArticles = collectNodes(
      bookLoop.body,
      (node) => node.type === "RegularElement" && node.name === "article",
    );
    expect(cardArticles).toHaveLength(1);

    const cardImages = collectNodes(
      cardArticles[0],
      (node) => node.type === "RegularElement" && node.name === "img",
    );
    expect(cardImages).toHaveLength(1);
    expect(hasAttribute(cardImages[0], "onerror")).toBe(true);

    const cardSnippet = source.slice(cardArticles[0].start, cardArticles[0].end);
    expect(cardSnippet).toContain("aspect-[2/3]");
    expect(cardSnippet).toContain("self-start");
    expect(cardSnippet).not.toContain("self-stretch");
    expect(cardSnippet).toContain("blur-[2px]");
    expect(cardSnippet).toContain("sm:blur-none");
  });

  it("renders 2:3 modal cover boxes with underlying BookOpen fallbacks and 100dvh height caps", () => {
    const source = readTemplate("../routes/+page.svelte");
    const modalPrimitiveSource = readTemplate("./components/ModalDialog.svelte");
    expect(source).not.toContain("100vh");
    expect(modalPrimitiveSource).not.toContain("100vh");
    expect(modalPrimitiveSource).toContain("max-h-[calc(100dvh-2rem)]");
    expect(modalPrimitiveSource).toContain("overflow-y-auto");

    const ast = parse(source, { modern: true }).fragment;

    const markReadDialog = collectNodes(
      ast,
      (node) =>
        node.type === "Component" &&
        node.name === "ModalDialog" &&
        staticAttribute(node, "ariaLabelledby") === "mark-read-title",
    )[0];
    expect(markReadDialog).toBeDefined();
    const markReadClass = staticAttribute(markReadDialog, "class") ?? "";
    expect(markReadClass).toContain("max-h-[min(42rem,calc(100dvh-2rem))]");

    const librarrDialog = collectNodes(
      ast,
      (node) =>
        node.type === "Component" &&
        node.name === "ModalDialog" &&
        staticAttribute(node, "ariaLabelledby") === "librarr-search-title",
    )[0];
    expect(librarrDialog).toBeDefined();
    const librarrClass = staticAttribute(librarrDialog, "class") ?? "";
    expect(librarrClass).toContain("max-h-[min(44rem,calc(100dvh-2rem))]");

    for (const dialogNode of [markReadDialog, librarrDialog]) {
      const modalImages = collectNodes(
        dialogNode,
        (node) => node.type === "RegularElement" && node.name === "img",
      );
      expect(modalImages).toHaveLength(1);
      expect(hasAttribute(modalImages[0], "onerror")).toBe(true);

      const coverBox = collectNodes(
        dialogNode,
        (node) =>
          node.type === "RegularElement" &&
          node.name === "div" &&
          node.start < modalImages[0].start &&
          node.end >= modalImages[0].end &&
          (staticAttribute(node, "class") ?? "").includes("aspect-[2/3]"),
      ).at(-1);
      expect(coverBox).toBeDefined();
      const coverBoxClass = staticAttribute(coverBox!, "class") ?? "";
      expect(coverBoxClass).toContain("w-12");
      expect(coverBoxClass).toContain("h-18");
      expect(coverBoxClass).not.toContain("size-16");
      expect(coverBoxClass).not.toContain("size-20");

      const directBookOpen = (coverBox!.fragment?.nodes ?? []).some(
        (child) => child.type === "Component" && child.name === "BookOpen",
      );
      expect(directBookOpen).toBe(true);
    }
  });
});
