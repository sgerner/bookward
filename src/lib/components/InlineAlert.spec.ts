import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import InlineAlert from "./InlineAlert.svelte";

describe("InlineAlert component", () => {
  it("renders visible error text with role=alert and tonal error styling", () => {
    const { body } = render(InlineAlert, {
      props: { message: "Recommendations <could> & not be loaded." },
    });

    expect(body).toContain('role="alert"');
    expect(body).toContain("preset-tonal-error");
    expect(body).toContain("Recommendations &lt;could> &amp; not be loaded.");
  });

  it("does not render an empty alert region when message is blank", () => {
    const { body } = render(InlineAlert, {
      props: { message: "   " },
    });

    expect(body).not.toContain('role="alert"');
  });
});
