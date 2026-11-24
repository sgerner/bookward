import { createRawSnippet } from "svelte";
import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import PopoverDialog from "./PopoverDialog.svelte";
import ThemePicker from "./ThemePicker.svelte";

const triggerSnippet = createRawSnippet(() => ({
  render: () => "<span>Open</span>",
}));

const panelSnippet = createRawSnippet(() => ({
  render: () => '<a href="/account">Account settings</a>',
}));

describe("PopoverDialog", () => {
  it("keeps a uniquely controlled hidden dialog mounted when closed", () => {
    const { body } = render(PopoverDialog, {
      props: {
        triggerLabel: "Open menu",
        dialogLabel: "Menu",
        trigger: triggerSnippet,
        children: panelSnippet,
      },
    });

    const controlsMatch = body.match(/aria-controls="([^"]+)"/);
    expect(controlsMatch).not.toBeNull();
    const panelId = controlsMatch![1];

    expect(body).toContain('aria-haspopup="dialog"');
    expect(body).toContain('aria-expanded="false"');
    expect(body).toContain(`id="${panelId}"`);
    expect(body).toContain('role="dialog"');
    expect(body).toContain('aria-label="Menu"');
    expect(body).toMatch(new RegExp(`<div[^>]*id="${panelId}"[^>]*hidden`));
  });

  it("supports an explicit panelId and unhidden open state", () => {
    const { body } = render(PopoverDialog, {
      props: {
        open: true,
        panelId: "profile-menu-panel",
        triggerLabel: "Open profile menu",
        dialogLabel: "Profile",
        trigger: triggerSnippet,
        children: panelSnippet,
      },
    });

    expect(body).toContain('aria-controls="profile-menu-panel"');
    expect(body).toContain('aria-expanded="true"');
    expect(body).toContain('id="profile-menu-panel"');
    expect(body).toContain('aria-label="Profile"');
    expect(body).not.toMatch(/<div[^>]*id="profile-menu-panel"[^>]*hidden/);
  });

  it("renders ThemePicker with a mounted named dialog in both standard and compact modes", () => {
    for (const compact of [false, true]) {
      const { body } = render(ThemePicker, { props: { compact } });

      const controlsMatch = body.match(/aria-controls="([^"]+)"/);
      const labelledByMatch = body.match(/aria-labelledby="([^"]+)"/);
      expect(controlsMatch).not.toBeNull();
      expect(labelledByMatch).not.toBeNull();

      const panelId = controlsMatch![1];
      const headingId = labelledByMatch![1];

      expect(body).toContain('aria-haspopup="dialog"');
      expect(body).toContain('aria-expanded="false"');
      expect(body).toContain(`id="${panelId}"`);
      expect(body).toContain('role="dialog"');
      expect(body).toContain(`<h2 id="${headingId}" class="text-lg font-semibold">Appearance</h2>`);
      expect(body).toMatch(new RegExp(`<div[^>]*id="${panelId}"[^>]*hidden`));
    }
  });
});
