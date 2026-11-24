import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import SwitchField from "./SwitchField.svelte";

describe("server-rendered SwitchField", () => {
  it("pairs the switch control with its visible label and links helper text via aria-describedby", () => {
    const { body } = render(SwitchField, {
      props: {
        id: "digest-discord",
        name: "channels",
        value: "discord",
        label: "Discord",
        description: "Post to a webhook",
        checked: true,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).not.toMatch(/<input[^>]*role="switch"/);
    expect(body).toContain('id="digest-discord"');
    expect(body).toContain('type="button"');
    expect(body).toContain('role="switch"');
    expect(body).toContain('aria-checked="true"');
    expect(body).toContain('aria-labelledby="digest-discord-label"');
    expect(body).toContain('aria-describedby="digest-discord-description"');
    expect(body).toContain('<input type="hidden" name="channels" value="discord"');
    expect(body).toMatch(
      /<label[^>]*id="digest-discord-label"[^>]*for="digest-discord"[^>]*>Discord<\/label>/,
    );
    expect(body).toMatch(
      /<p[^>]*id="digest-discord-description"[^>]*>Post to a webhook<\/p>/,
    );
  });

  it("omits aria-describedby and hidden input when unchecked without helper description", () => {
    const { body } = render(SwitchField, {
      props: {
        id: "only-new",
        name: "onlyNew",
        label: "Only send new books",
        checked: false,
      },
    });

    expect(body).not.toContain("aria-label=");
    expect(body).not.toContain("aria-describedby=");
    expect(body).not.toContain("<input");
    expect(body).toContain('aria-checked="false"');
    expect(body).toContain('aria-labelledby="only-new-label"');
    expect(body).toMatch(
      /<label[^>]*id="only-new-label"[^>]*for="only-new"[^>]*>Only send new books<\/label>/,
    );
  });
});
