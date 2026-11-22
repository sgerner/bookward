import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import SwitchField from "./SwitchField.svelte";

describe("server-rendered SwitchField", () => {
  it("pairs the switch input with its visible label and links helper text via aria-describedby", () => {
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
    expect(body).toContain('id="digest-discord"');
    expect(body).toContain('role="switch"');
    expect(body).toMatch(/<input[^>]*class="sr-only"[^>]*role="switch"/);
    expect(body).toContain('name="channels"');
    expect(body).toContain('value="discord"');
    expect(body).toContain('aria-describedby="digest-discord-description"');
    expect(body).toMatch(/<label[^>]*for="digest-discord"[^>]*>Discord<\/label>/);
    expect(body).toMatch(
      /<p[^>]*id="digest-discord-description"[^>]*>Post to a webhook<\/p>/,
    );
  });

  it("omits aria-describedby when no helper description is provided", () => {
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
    expect(body).toMatch(
      /<label[^>]*for="only-new"[^>]*>Only send new books<\/label>/,
    );
  });
});
