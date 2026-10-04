import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import ProfileField from "./ProfileField.svelte";

describe("server-rendered profile binding field", () => {
  it("renders the expected profile into authenticated forms", () => {
    const { body } = render(ProfileField, {
      props: { profileId: "reader<&one" },
    });

    expect(body).toContain('type="hidden" name="__profile_id"');
    expect(body).toContain('value="reader&lt;&amp;one"');
  });

  it("does not render a blank profile binding for unauthenticated pages", () => {
    const { body } = render(ProfileField, { props: { profileId: null } });

    expect(body).not.toContain("<input");
  });
});
