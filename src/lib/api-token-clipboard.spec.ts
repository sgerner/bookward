import { describe, expect, it, vi } from "vitest";
import {
  COPY_FAILURE_MESSAGE,
  COPY_SUCCESS_MESSAGE,
  copyApiTokenText,
} from "./api-token-clipboard";

describe("API token clipboard feedback", () => {
  it("reports polite status metadata after writing the token", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const result = await copyApiTokenText("bkw_secret", { writeText });
    expect(result).toEqual({
      ok: true,
      error: false,
      role: "status",
      politeness: "polite",
      message: COPY_SUCCESS_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
    expect(writeText).toHaveBeenCalledWith("bkw_secret");
  });

  it("reports an assertive alert fallback when the clipboard is unavailable", async () => {
    const result = await copyApiTokenText("bkw_secret", undefined);
    expect(result).toEqual({
      ok: false,
      error: true,
      role: "alert",
      politeness: "assertive",
      message: COPY_FAILURE_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
  });

  it("reports an assertive alert fallback when the token is empty", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const result = await copyApiTokenText("", { writeText });
    expect(result).toEqual({
      ok: false,
      error: true,
      role: "alert",
      politeness: "assertive",
      message: COPY_FAILURE_MESSAGE,
    });
    expect(writeText).not.toHaveBeenCalled();
  });

  it("reports an assertive alert fallback when the write is rejected", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    const result = await copyApiTokenText("bkw_secret", { writeText });
    expect(result).toEqual({
      ok: false,
      error: true,
      role: "alert",
      politeness: "assertive",
      message: COPY_FAILURE_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
  });
});
