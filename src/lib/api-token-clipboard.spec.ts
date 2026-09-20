import { describe, expect, it, vi } from "vitest";
import {
  COPY_FAILURE_MESSAGE,
  COPY_SUCCESS_MESSAGE,
  copyApiTokenText,
} from "./api-token-clipboard";

describe("API token clipboard feedback", () => {
  it("reports success after writing the token", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    await expect(copyApiTokenText("bkw_secret", { writeText })).resolves.toBe(
      COPY_SUCCESS_MESSAGE,
    );
    expect(writeText).toHaveBeenCalledWith("bkw_secret");
  });

  it("reports a manual-copy fallback when the clipboard is unavailable", async () => {
    await expect(copyApiTokenText("bkw_secret", undefined)).resolves.toBe(
      COPY_FAILURE_MESSAGE,
    );
  });

  it("reports a manual-copy fallback when the write is rejected", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    await expect(copyApiTokenText("bkw_secret", { writeText })).resolves.toBe(
      COPY_FAILURE_MESSAGE,
    );
  });
});
