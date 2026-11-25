import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createRawSnippet } from "svelte";
import { compile } from "svelte/compiler";
import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import InlineAlert from "./InlineAlert.svelte";

function extractClassTokens(html: string): string[] {
  const match = html.match(/class="([^"]+)"/);
  return match ? match[1].trim().split(/\s+/) : [];
}

describe("InlineAlert component", () => {
  it("renders visible error text with role=alert, tonal error styling, and default md sizing", () => {
    const { body } = render(InlineAlert, {
      props: { message: "Recommendations <could> & not be loaded." },
    });

    const classes = extractClassTokens(body);
    expect(body).toContain('role="alert"');
    expect(classes).toContain("preset-tonal-error");
    expect(classes).toContain("p-4");
    expect(classes).toContain("text-sm");
    expect(classes).not.toContain("p-3");
    expect(classes).not.toContain("text-xs");
    expect(body).toContain("Recommendations &lt;could> &amp; not be loaded.");
  });

  it("renders compact sm sizing without colliding with md padding or font-size utilities", () => {
    const { body } = render(InlineAlert, {
      props: {
        message: "Copy failed. Select and copy the token manually.",
        size: "sm",
        class: "mb-3",
      },
    });

    const classes = extractClassTokens(body);
    expect(body).toContain('role="alert"');
    expect(classes).toContain("p-3");
    expect(classes).toContain("text-xs");
    expect(classes).toContain("mb-3");
    expect(classes).not.toContain("p-4");
    expect(classes).not.toContain("text-sm");
  });

  it("supports warning tone styling and custom children snippets", () => {
    const warning = render(InlineAlert, {
      props: { message: "Rate limit approaching.", tone: "warning" },
    });

    expect(extractClassTokens(warning.body)).toContain("preset-tonal-warning");
    expect(extractClassTokens(warning.body)).not.toContain("preset-tonal-error");

    const children = createRawSnippet(() => ({
      render: () => "<span>Structured alert detail</span>",
    }));
    const rich = render(InlineAlert, {
      props: { size: "sm", tone: "warning", children },
    });
    const richClasses = extractClassTokens(rich.body);
    expect(rich.body).toContain('<div class="');
    expect(rich.body).toContain('role="alert"');
    expect(rich.body).toContain("<span>Structured alert detail</span>");
    expect(richClasses).toContain("p-3");
    expect(richClasses).toContain("text-xs");
    expect(richClasses).toContain("preset-tonal-warning");
    expect(richClasses).not.toContain("p-4");
    expect(richClasses).not.toContain("text-sm");
  });

  it("does not render an empty alert region when message is blank or null", () => {
    expect(render(InlineAlert, { props: { message: "   " } }).body).not.toContain(
      'role="alert"',
    );
    expect(render(InlineAlert, { props: { message: null } }).body).not.toContain(
      'role="alert"',
    );
  });

  it("compiles cleanly in client runes mode with reactive tone, size, and trimmed message branches", () => {
    const source = readFileSync(
      fileURLToPath(new URL("./InlineAlert.svelte", import.meta.url)),
      "utf8",
    );
    const compiled = compile(source, {
      filename: "InlineAlert.svelte",
      generate: "client",
      runes: true,
    });

    expect(compiled.warnings).toEqual([]);
    expect(compiled.js.code).toContain("preset-tonal-warning");
    expect(compiled.js.code).toContain("preset-tonal-error");
    expect(compiled.js.code).toContain("p-3 text-xs");
    expect(compiled.js.code).toContain("p-4 text-sm");
  });
});
