import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "svelte/compiler";

type ElementMatch = {
  name: string;
  start: number;
  end: number;
  attributes: Map<string, string>;
};

const standalonePages = [
  "../routes/account/+page.svelte",
  "../routes/admin/+page.svelte",
  "../routes/login/+page.svelte",
  "../routes/+error.svelte",
];

function readRelative(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

function collectElements(node: unknown): ElementMatch[] {
  if (!node || typeof node !== "object") return [];
  const record = node as Record<string, unknown>;
  const found: ElementMatch[] = [];

  if (
    (record.type === "RegularElement" || record.type === "Component") &&
    typeof record.name === "string"
  ) {
    const rawAttributes = (record.attributes as Array<{
      type: string;
      name?: string;
      value?: true | Array<{ type?: string; data?: string }>;
    }>) ?? [];
    const attributes = new Map<string, string>();
    for (const attribute of rawAttributes) {
      if (attribute.type !== "Attribute" || !attribute.name) continue;
      if (attribute.value === true || !Array.isArray(attribute.value)) {
        attributes.set(attribute.name, "");
      } else {
        attributes.set(
          attribute.name,
          attribute.value.map((part) => part.data ?? "").join(""),
        );
      }
    }
    found.push({
      name: record.name,
      start: record.start as number,
      end: record.end as number,
      attributes,
    });
  }

  for (const value of Object.values(record)) {
    if (Array.isArray(value)) {
      for (const child of value) found.push(...collectElements(child));
    } else if (value && typeof value === "object") {
      found.push(...collectElements(value));
    }
  }
  return found;
}

describe("sticky shell scroll padding (WCAG 2.2 C43)", () => {
  it("scopes data-sticky-shell to the reader shell and avoids double Librarr scroll offsets", () => {
    const readerSource = readRelative("../routes/+page.svelte");
    const elements = collectElements(parse(readerSource, { modern: true }).fragment);

    const stickyShells = elements.filter((el) => el.attributes.has("data-sticky-shell"));
    expect(stickyShells).toHaveLength(1);
    const [shell] = stickyShells;

    const stickyHeader = elements.find(
      (el) => el.name === "header" && (el.attributes.get("class") ?? "").includes("sticky top-0"),
    );
    const mainContent = elements.find(
      (el) => el.name === "main" && el.attributes.get("id") === "main-content",
    );
    const mobileNav = elements.find(
      (el) =>
        el.name === "nav" &&
        el.attributes.get("aria-label") === "Mobile navigation" &&
        (el.attributes.get("class") ?? "").includes("md:hidden"),
    );

    expect(stickyHeader, "reader shell should contain sticky top header").toBeDefined();
    expect(mainContent, "reader shell should contain #main-content").toBeDefined();
    expect(mobileNav, "reader shell should contain md:hidden mobile nav").toBeDefined();

    for (const landmark of [stickyHeader!, mainContent!, mobileNav!]) {
      expect(landmark.start).toBeGreaterThanOrEqual(shell.start);
      expect(landmark.end).toBeLessThanOrEqual(shell.end);
    }

    const librarrForm = elements.find((el) => el.attributes.get("id") === "librarr-settings");
    expect(librarrForm, "#librarr-settings form should exist").toBeDefined();
    const librarrClasses = (librarrForm!.attributes.get("class") ?? "").split(/\s+/);
    expect(librarrClasses).toContain("scroll-mt-4");
    expect(librarrClasses).not.toContain("scroll-mt-24");

    const srOnlySwitches = elements.filter((el) => el.name === "SwitchControl");
    expect(srOnlySwitches.length).toBeGreaterThanOrEqual(4);
    for (const control of srOnlySwitches) {
      expect(control.start).toBeGreaterThanOrEqual(shell.start);
      expect(control.end).toBeLessThanOrEqual(shell.end);
    }

    for (const page of standalonePages) {
      const standaloneElements = collectElements(
        parse(readRelative(page), { modern: true }).fragment,
      );
      const leaked = standaloneElements.filter((el) => el.attributes.has("data-sticky-shell"));
      expect(leaked, `${page} should not set data-sticky-shell`).toEqual([]);
    }
  });

  it("defines scoped root scroll-padding and switch scroll-margin-block rules in layout.css", () => {
    const css = readRelative("../routes/layout.css");

    expect(css).toMatch(
      /html:has\(\[data-sticky-shell\]\)\s*\{[^}]*scroll-padding-top:\s*calc\(5rem\s*\+\s*env\(safe-area-inset-top,\s*0px\)\);[^}]*scroll-padding-bottom:\s*calc\(5rem\s*\+\s*env\(safe-area-inset-bottom,\s*0px\)\);[^}]*\}/s,
    );
    expect(css).toMatch(
      /@media\s*\(min-width:\s*48rem\)\s*\{\s*html:has\(\[data-sticky-shell\]\)\s*\{\s*scroll-padding-bottom:\s*0;\s*\}\s*\}/s,
    );
    expect(css).toMatch(
      /\[data-sticky-shell\]\s+\[role=["']switch["']\]\s*\{\s*scroll-margin-block:\s*4rem;\s*\}/s,
    );
    expect(css).not.toMatch(/(?:^|\n)\s*html\s*\{[^}]*scroll-padding/s);
  });
});
