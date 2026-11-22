import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import StatusRegion from "./StatusRegion.svelte";

describe("StatusRegion component", () => {
  it("renders a pre-mounted polite status region with suppressed initial mount text", () => {
    const { body } = render(StatusRegion, {
      props: { message: "Showing 8 recommendations." },
    });

    expect(body).toContain('role="status"');
    expect(body).toContain('aria-live="polite"');
    expect(body).toContain('aria-atomic="true"');
    expect(body).toContain('class="sr-only"');
    expect(body).not.toContain("Showing 8 recommendations.");
  });

  it("accepts a custom class while preserving ARIA status semantics", () => {
    const { body } = render(StatusRegion, {
      props: { message: "Copied to clipboard.", class: "text-xs" },
    });

    expect(body).toContain('class="text-xs"');
    expect(body).toContain('role="status"');
  });
});
