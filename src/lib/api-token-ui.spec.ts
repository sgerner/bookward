import { describe, expect, it } from "vitest";
import { tokenForView } from "./api-token-ui";

describe("API token reveal state", () => {
  it("clears the one-time token outside Settings", () => {
    expect(tokenForView("discover", "bkw_secret")).toBeNull();
    expect(tokenForView("saved", "bkw_secret")).toBeNull();
    expect(tokenForView("sources", "bkw_secret")).toBeNull();
  });

  it("retains the token while Settings remains active", () => {
    expect(tokenForView("settings", "bkw_secret")).toBe("bkw_secret");
  });
});
