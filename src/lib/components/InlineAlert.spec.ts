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

  it("supports warning tone styling", () => {
    const { body } = render(InlineAlert, {
      props: { message: "Rate limit approaching.", tone: "warning" },
    });

    expect(extractClassTokens(body)).toContain("preset-tonal-warning");
    expect(extractClassTokens(body)).not.toContain("preset-tonal-error");
  });

  it("does not render an empty alert region when message is blank", () => {
    const { body } = render(InlineAlert, {
      props: { message: "   " },
    });

    expect(body).not.toContain('role="alert"');
  });
});
