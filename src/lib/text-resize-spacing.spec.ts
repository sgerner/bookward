import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "svelte/compiler";

type AstElement = {
  name: string;
  start: number;
  end: number;
  attributes: Map<string, string>;
  classTokens: Set<string>;
};

function readSource(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

function attributeText(
  attribute: Record<string, unknown>,
  source: string,
): string {
  const value = attribute.value;
  if (value === true) return "true";
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (!part || typeof part !== "object") return "";
        const record = part as Record<string, unknown>;
        if (typeof record.data === "string") return record.data;
        if (typeof record.start === "number" && typeof record.end === "number") {
          return source.slice(record.start, record.end);
        }
        return "";
      })
      .join(" ");
  }
  if (
    value &&
    typeof value === "object" &&
    typeof (value as Record<string, unknown>).start === "number" &&
    typeof (value as Record<string, unknown>).end === "number"
  ) {
    const span = value as { start: number; end: number };
    return source.slice(span.start, span.end);
  }
  return "";
}

function collectElements(node: unknown, source: string): AstElement[] {
  if (!node || typeof node !== "object") return [];
  const record = node as Record<string, unknown>;
  const found: AstElement[] = [];

  if (record.type === "RegularElement" && typeof record.name === "string") {
    const attributes = new Map<string, string>();
    const rawAttributes = Array.isArray(record.attributes)
      ? (record.attributes as Array<Record<string, unknown>>)
      : [];

    for (const attribute of rawAttributes) {
      if (attribute.type === "Attribute" && typeof attribute.name === "string") {
        attributes.set(attribute.name, attributeText(attribute, source));
      } else if (
        attribute.type === "UseDirective" &&
        typeof attribute.name === "string"
      ) {
        attributes.set(`use:${attribute.name}`, "true");
      }
    }

    const classAttr = attributes.get("class") ?? "";
    const classTokens = new Set(
      classAttr
        .replace(/[`${}?:"'()]/g, " ")
        .split(/\s+/)
        .filter(Boolean),
    );

    found.push({
      name: record.name,
      start: record.start as number,
      end: record.end as number,
      attributes,
      classTokens,
    });
  }

  for (const value of Object.values(record)) {
    if (Array.isArray(value)) {
      for (const child of value) found.push(...collectElements(child, source));
    } else if (value && typeof value === "object") {
      found.push(...collectElements(value, source));
    }
  }
  return found;
}

describe("WCAG 1.4.4 resize text and 1.4.12 text spacing reflow", () => {
  it("defines shared modal scroll and text reflow utilities in layout.css", () => {
    const css = readSource("../routes/layout.css");

    for (const utility of [
      ".modal-scroll-shell",
      ".modal-scroll-body",
      ".text-reflow",
      ".url-reflow",
    ]) {
      expect(css, `layout.css should define ${utility}`).toContain(utility);
    }

    expect(css).toMatch(/\.modal-scroll-shell\s*\{[^}]*display:\s*flex/s);
    expect(css).toMatch(/\.modal-scroll-shell\s*\{[^}]*flex-direction:\s*column/s);
    expect(css).toMatch(/\.modal-scroll-shell\s*\{[^}]*100vh/s);
    expect(css).toMatch(/\.modal-scroll-shell\s*\{[^}]*100dvh/s);
    expect(css).toMatch(/\.modal-scroll-body\s*\{[^}]*min-height:\s*0/s);
    expect(css).toMatch(/\.modal-scroll-body\s*\{[^}]*overflow-y:\s*auto/s);
    expect(css).toMatch(/\.text-reflow\s*\{[^}]*overflow-wrap:\s*anywhere/s);
    expect(css).toMatch(/\.url-reflow\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  });

  it("allows the header row and Primary navigation to wrap without obstructing action buttons", () => {
    const source = readSource("../routes/+page.svelte");
    const elements = collectElements(parse(source, { modern: true }).fragment, source);

    const header = elements.find((el) => el.name === "header");
    expect(header, "+page.svelte should render a sticky header").toBeDefined();

    const headerChildren = elements.filter(
      (el) => header && el.start > header.start && el.end < header.end,
    );
    const headerRow = headerChildren.find(
      (el) => el.name === "div" && el.classTokens.has("max-w-7xl"),
    );
    expect(headerRow, "header inner row should exist").toBeDefined();
    expect(headerRow?.classTokens.has("flex-wrap")).toBe(true);
    expect(headerRow?.classTokens.has("py-2")).toBe(true);

    const primaryNav = headerChildren.find(
      (el) =>
        el.name === "nav" &&
        el.attributes.get("aria-label") === "Primary navigation",
    );
    expect(primaryNav, "Primary navigation should exist").toBeDefined();
    expect(primaryNav?.classTokens.has("flex-wrap")).toBe(true);
    expect(primaryNav?.classTokens.has("py-1")).toBe(true);

    const headerActionGroup = headerChildren.find(
      (el) => el.name === "div" && el.classTokens.has("ml-auto"),
    );
    expect(headerActionGroup?.classTokens.has("shrink-0")).toBe(true);
  });

  it("uses flex-column modal scroll shells and bodies on Librarr and Mark Read dialogs", () => {
    const source = readSource("../routes/+page.svelte");
    const elements = collectElements(parse(source, { modern: true }).fragment, source);

    const markReadDialog = elements.find(
      (el) => el.attributes.get("aria-labelledby") === "mark-read-title",
    );
    expect(markReadDialog).toBeDefined();
    expect(markReadDialog?.attributes.get("use:modalFocus")).toBe("true");
    expect(markReadDialog?.classTokens.has("modal-scroll-shell")).toBe(true);

    const markReadSlice = markReadDialog
      ? source.slice(markReadDialog.start, markReadDialog.end)
      : "";
    expect(markReadSlice).toContain("modal-scroll-body");

    const librarrDialog = elements.find(
      (el) =>
        el.name === "dialog" &&
        el.attributes.get("aria-labelledby") === "librarr-search-title",
    );
    expect(librarrDialog).toBeDefined();
    expect(librarrDialog?.attributes.get("use:modalFocus")).toBe("true");
    expect(librarrDialog?.classTokens.has("modal-scroll-shell")).toBe(true);

    const librarrSlice = librarrDialog
      ? source.slice(librarrDialog.start, librarrDialog.end)
      : "";
    expect(librarrSlice).toContain("modal-scroll-body");
    expect(librarrSlice).toContain("min-h-0");
    expect(librarrSlice).toContain("flex-1");
    expect(librarrSlice).toContain("overflow-y-auto");
    expect(source).not.toContain("max-h-[calc(100vh-11rem)]");
  });

  it("eliminates truncate, line-clamp, and fixed leading-5 classes across +page.svelte and ThemePicker.svelte", () => {
    for (const file of [
      "../routes/+page.svelte",
      "./components/ThemePicker.svelte",
    ]) {
      const source = readSource(file);
      const elements = collectElements(parse(source, { modern: true }).fragment, source);

      const offending = elements.flatMap((el) =>
        [...el.classTokens]
          .filter(
            (token) =>
              token === "truncate" ||
              token.startsWith("line-clamp-") ||
              token === "leading-5",
          )
          .map((token) => `${el.name}:${token}`),
      );

      expect(offending, `${file} should not contain truncation or fixed leading-5 utilities`).toEqual([]);
    }

    const pageSource = readSource("../routes/+page.svelte");
    expect(pageSource).toContain("text-reflow");
    expect(pageSource).toContain("url-reflow");
  });

  it("uses em-based auto-fit palette columns without nested height caps in ThemePicker.svelte", () => {
    const source = readSource("./components/ThemePicker.svelte");
    const elements = collectElements(parse(source, { modern: true }).fragment, source);

    const appearancePanel = elements.find(
      (el) => el.attributes.get("id") === "appearance-panel",
    );
    expect(appearancePanel?.classTokens.has("modal-scroll-shell")).toBe(true);

    const paletteGroup = elements.find(
      (el) => el.attributes.get("aria-label") === "Skeleton theme",
    );
    expect(paletteGroup).toBeDefined();

    const paletteClasses = paletteGroup?.attributes.get("class") ?? "";
    expect(paletteClasses).toMatch(/repeat\(auto-fit,minmax\(min\(100%,\d+(?:\.\d+)?em\),1fr\)\)/);
    expect(paletteGroup?.classTokens.has("grid-cols-2")).toBe(false);
    expect(paletteClasses).not.toContain("max-h-");
    expect(paletteGroup?.classTokens.has("overflow-y-auto")).toBe(false);
    expect(source).toContain("event.key === 'Escape'");
  });
});
