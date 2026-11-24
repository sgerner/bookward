import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "svelte/compiler";

const authenticatedPages = [
  "../routes/+page.svelte",
  "../routes/account/+page.svelte",
  "../routes/admin/+page.svelte",
];

function postForms(node: unknown): Array<{ start: number; end: number }> {
  if (!node || typeof node !== "object") return [];
  const record = node as Record<string, unknown>;
  const found: Array<{ start: number; end: number }> = [];

  if (record.type === "RegularElement" && record.name === "form") {
    const attributes = record.attributes as Array<{
      type: string;
      name?: string;
      value?: Array<{ data?: string }>;
    }>;
    const method = attributes.find(
      (attribute) => attribute.type === "Attribute" && attribute.name === "method",
    );
    if (method?.value?.map((part) => part.data ?? "").join("").toLowerCase() === "post") {
      found.push({ start: record.start as number, end: record.end as number });
    }
  }

  for (const value of Object.values(record)) {
    if (Array.isArray(value)) {
      for (const child of value) found.push(...postForms(child));
    } else if (value && typeof value === "object") {
      found.push(...postForms(value));
    }
  }
  return found;
}

describe("authenticated native forms", () => {
  it("render a profile binding in every POST form before client hydration", () => {
    for (const page of authenticatedPages) {
      const source = readFileSync(fileURLToPath(new URL(page, import.meta.url)), "utf8");
      const forms = postForms(parse(source, { modern: true }).fragment);

      expect(forms.length, `${page} should contain authenticated POST forms`).toBeGreaterThan(0);
      const missing = forms.filter(
        ({ start, end }) => !source.slice(start, end).includes("<ProfileField"),
      );
      expect(missing, `${page} has a POST form without __profile_id`).toEqual([]);
    }
  });
});
