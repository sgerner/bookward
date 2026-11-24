import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import ChipToggle from "./components/ChipToggle.svelte";

type AstNode = {
  type?: string;
  name?: string;
  start?: number;
  end?: number;
  attributes?: Array<{
    type: string;
    name?: string;
    value?: true | Array<{ type?: string; data?: string }>;
  }>;
  [key: string]: unknown;
};

function readTemplate(relativePath: string) {
  const source = readFileSync(
    fileURLToPath(new URL(relativePath, import.meta.url)),
    "utf8",
  );
  const ast = parse(source, { modern: true });
  return { source, fragment: ast.fragment as unknown as AstNode };
}

function collectElements(
  node: unknown,
  tagName?: string,
  ancestors: AstNode[] = [],
): Array<{ element: AstNode; ancestors: AstNode[] }> {
  if (!node || typeof node !== "object") return [];
  const record = node as AstNode;
  const found: Array<{ element: AstNode; ancestors: AstNode[] }> = [];
  const isRegular = record.type === "RegularElement";

  if (isRegular && (!tagName || record.name === tagName)) {
    found.push({ element: record, ancestors });
  }

  const nextAncestors = isRegular ? [...ancestors, record] : ancestors;
  for (const [key, value] of Object.entries(record)) {
    if (key === "attributes") continue;
    if (Array.isArray(value)) {
      for (const child of value) {
        found.push(...collectElements(child, tagName, nextAncestors));
      }
    } else if (value && typeof value === "object") {
      found.push(...collectElements(value, tagName, nextAncestors));
    }
  }
  return found;
}

function getStaticAttr(element: AstNode, attrName: string): string {
  const attr = element.attributes?.find(
    (item) => item.type === "Attribute" && item.name === attrName,
  );
  if (!attr) return "";
  if (attr.value === true) return "true";
  if (!Array.isArray(attr.value)) return "";
  return attr.value.map((part) => part.data ?? "").join(" ");
}

function getRawAttrSlice(source: string, element: AstNode, attrName: string): string {
  const attr = element.attributes?.find(
    (item) => item.type === "Attribute" && item.name === attrName,
  ) as { start?: number; end?: number } | undefined;
  if (!attr || typeof attr.start !== "number" || typeof attr.end !== "number") return "";
  return source.slice(attr.start, attr.end);
}

describe("mobile target sizes and modal bounds", () => {
  const pages = [
    "../routes/+page.svelte",
    "../routes/account/+page.svelte",
    "../routes/admin/+page.svelte",
    "./components/ThemePicker.svelte",
    "./components/ChipToggle.svelte",
  ];

  it("keeps buttons and links outside label elements for valid HTML5", () => {
    for (const page of pages) {
      const { source, fragment } = readTemplate(page);
      const labels = collectElements(fragment, "label");
      for (const { element: label } of labels) {
        const nestedInteractive = [
          ...collectElements(label, "button").filter((item) => item.element !== label),
          ...collectElements(label, "a").filter((item) => item.element !== label),
        ];
        expect(
          nestedInteractive.map(
            ({ element }) => source.slice(element.start ?? 0, element.end ?? 0),
          ),
          `${page} must not nest <button> or <a> inside <label>`,
        ).toEqual([]);
      }
    }
  });

  it("wraps search input-ghost controls inside full-height labels", () => {
    const { source, fragment } = readTemplate("../routes/+page.svelte");
    const ghostInputs = collectElements(fragment, "input").filter(({ element }) =>
      getRawAttrSlice(source, element, "class").includes("input-ghost"),
    );

    expect(ghostInputs.length).toBe(2);
    for (const { element, ancestors } of ghostInputs) {
      const inputClass = getRawAttrSlice(source, element, "class");
      expect(inputClass).toMatch(/\bmin-h-(11|12)\b/);
      expect(inputClass).toContain("self-stretch");

      const parentLabel = [...ancestors]
        .reverse()
        .find((ancestor) => ancestor.name === "label");
      expect(parentLabel, "input-ghost must be wrapped by a <label>").toBeDefined();
      const labelClass = getRawAttrSlice(source, parentLabel!, "class");
      expect(labelClass).toMatch(/\bmin-h-(11|12)\b/);
    }
  });

  it("enforces >=32px touch targets on genre chips, source links, source lifecycle actions, and filter summaries", () => {
    const { source, fragment } = readTemplate("../routes/+page.svelte");

    expect(source).toContain("<ChipToggle");
    const chipHtml = render(ChipToggle, {
      props: { label: "Sci-Fi", ariaLabel: "Sci-Fi filter" },
    }).body;
    expect(chipHtml).toContain("min-h-8");
    expect(chipHtml).toContain("min-w-8");
    expect(chipHtml).toContain("px-2.5");

    const provenanceLinks = collectElements(fragment, "a").filter(({ element }) =>
      getRawAttrSlice(source, element, "aria-label").includes("Open source for"),
    );
    expect(provenanceLinks.length).toBe(1);
    for (const { element } of provenanceLinks) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(cls).toContain("min-h-8");
      expect(cls).toContain("min-w-8");
    }

    const switchButtons = collectElements(fragment, "button").filter(({ element }) =>
      source.slice(element.start ?? 0, element.end ?? 0).includes("Switch to One-Time Import"),
    );
    expect(switchButtons.length).toBe(1);
    expect(getRawAttrSlice(source, switchButtons[0].element, "class")).toContain("min-h-8");

    const summaries = collectElements(fragment, "summary");
    expect(summaries.length).toBeGreaterThanOrEqual(4);
    for (const { element } of summaries) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(
        /\bmin-h-(8|9|10|11|12)\b/.test(cls),
        `<summary> missing >=32px min-height: ${cls}`,
      ).toBe(true);
    }
  });

  it("sizes visible checkboxes to 24x24px inside touch-friendly labels and external Settings links to >=24px", () => {
    const { source, fragment } = readTemplate("../routes/+page.svelte");

    const checkboxes = collectElements(fragment, "input").filter(
      ({ element }) => getStaticAttr(element, "type") === "checkbox",
    );
    const visibleCheckboxes = checkboxes.filter(
      ({ element }) => !getRawAttrSlice(source, element, "class").includes("sr-only"),
    );
    expect(visibleCheckboxes.length).toBe(6);

    for (const { element, ancestors } of visibleCheckboxes) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(cls, "visible checkbox must have size-6 (24x24px)").toContain("size-6");

      const parentLabel = [...ancestors]
        .reverse()
        .find((ancestor) => ancestor.name === "label");
      expect(parentLabel).toBeDefined();
      const labelClass = getRawAttrSlice(source, parentLabel!, "class");
      if (getStaticAttr(element, "name")) {
        expect(
          /\bmin-h-(10|11|12)\b/.test(labelClass),
          `Settings checkbox label must be at least 40px tall: ${labelClass}`,
        ).toBe(true);
      } else {
        // Digest Review selection checkbox label: flow on mobile, absolute on sm+
        expect(labelClass).toContain("size-8");
        expect(labelClass).toContain("sm:absolute");
        expect(labelClass).toContain("sm:left-3");
        expect(labelClass).toContain("sm:top-3");
        expect(labelClass).not.toMatch(/(^|\s|")absolute(\s|")/);
      }
    }

    const externalSettingsLinks = collectElements(fragment, "a").filter(({ element }) => {
      const href = getStaticAttr(element, "href");
      return href.startsWith("https://");
    });
    expect(externalSettingsLinks.length).toBe(2);
    for (const { element } of externalSettingsLinks) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(cls).toContain("inline-flex");
      expect(cls).toContain("min-h-6");
    }
  });

  it("provides 44px back links on Account and Admin routes", () => {
    for (const route of ["../routes/account/+page.svelte", "../routes/admin/+page.svelte"]) {
      const { source, fragment } = readTemplate(route);
      const backLinks = collectElements(fragment, "a").filter(
        ({ element }) => getStaticAttr(element, "href") === "/",
      );
      expect(backLinks.length, `${route} should render a home back link`).toBe(1);
      const cls = getRawAttrSlice(source, backLinks[0].element, "class");
      expect(cls).toContain("inline-flex");
      expect(cls).toContain("min-h-11");
      expect(cls).toContain("items-center");
    }
  });

  it("bounds Librarr Search dialog with flex layout and sizes modal close and acquisition buttons", () => {
    const { source, fragment } = readTemplate("../routes/+page.svelte");
    const collectComponents = (node: unknown, name: string): AstNode[] => {
      if (!node || typeof node !== "object") return [];
      const record = node as AstNode;
      const found: AstNode[] =
        record.type === "Component" && record.name === name ? [record] : [];
      for (const [key, value] of Object.entries(record)) {
        if (key === "attributes") continue;
        if (Array.isArray(value)) {
          for (const child of value) found.push(...collectComponents(child, name));
        } else if (value && typeof value === "object") {
          found.push(...collectComponents(value, name));
        }
      }
      return found;
    };
    const dialogs = collectComponents(fragment, "ModalDialog").filter(
      (node) => getStaticAttr(node, "ariaLabelledby") === "librarr-search-title",
    );
    expect(dialogs.length).toBe(1);

    const dialog = dialogs[0];
    const dialogClass = getRawAttrSlice(source, dialog, "class");
    expect(dialogClass).toContain("flex");
    expect(dialogClass).toContain("flex-col");
    const dialogSlice = source.slice(dialog.start ?? 0, dialog.end ?? 0);
    expect(dialogSlice).not.toContain("max-h-[calc(100vh-11rem)]");
    expect(dialogSlice).toContain("min-h-0");
    expect(dialogSlice).toContain("flex-1");
    expect(dialogSlice).toContain("overflow-y-auto");

    const closeButtons = collectElements(fragment, "button").filter(({ element }) => {
      const label = getStaticAttr(element, "aria-label");
      return label === "Close Mark Read dialog" || label === "Close Librarr search";
    });
    expect(closeButtons.length).toBe(2);
    for (const { element } of closeButtons) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(cls).toContain("min-h-9");
      expect(cls).toContain("min-w-9");
    }

    const themePicker = readTemplate("./components/ThemePicker.svelte");
    const pickerClose = collectElements(themePicker.fragment, "button").filter(
      ({ element }) => getStaticAttr(element, "aria-label") === "Close appearance picker",
    );
    expect(pickerClose.length).toBe(1);
    expect(getRawAttrSlice(themePicker.source, pickerClose[0].element, "class")).toContain("min-h-9");
    expect(getRawAttrSlice(themePicker.source, pickerClose[0].element, "class")).toContain("min-w-9");

    const librarrButtons = collectElements(fragment, "button").filter(({ element }) => {
      const raw = source.slice(element.start ?? 0, element.end ?? 0);
      return raw.includes("to Librarr`}") || raw.includes("Add high-confidence matches");
    });
    expect(librarrButtons.length).toBe(2);
    for (const { element } of librarrButtons) {
      const cls = getRawAttrSlice(source, element, "class");
      expect(cls).toContain("min-h-11");
    }
    const perResultAdd = librarrButtons.find(({ element }) =>
      source.slice(element.start ?? 0, element.end ?? 0).includes("to Librarr`}"),
    );
    expect(getRawAttrSlice(source, perResultAdd!.element, "class")).toContain("min-w-11");
  });
});
