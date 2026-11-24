import { describe, expect, it, vi } from "vitest";
import {
  COPY_FAILURE_MESSAGE,
  COPY_SUCCESS_MESSAGE,
  copyApiTokenText,
} from "./api-token-clipboard";

describe("API token clipboard feedback", () => {
  it("reports non-error copy feedback after writing the token", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const result = await copyApiTokenText("bkw_secret", { writeText });
    expect(result).toEqual({
      error: false,
      message: COPY_SUCCESS_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
    expect(writeText).toHaveBeenCalledWith("bkw_secret");
  });

  it("reports error copy feedback when the clipboard is unavailable", async () => {
    const result = await copyApiTokenText("bkw_secret", undefined);
    expect(result).toEqual({
      error: true,
      message: COPY_FAILURE_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
  });

  it("reports error copy feedback when the token is empty", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const result = await copyApiTokenText("", { writeText });
    expect(result).toEqual({
      error: true,
      message: COPY_FAILURE_MESSAGE,
    });
    expect(writeText).not.toHaveBeenCalled();
  });

  it("reports error copy feedback when the write is rejected", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    const result = await copyApiTokenText("bkw_secret", { writeText });
    expect(result).toEqual({
      error: true,
      message: COPY_FAILURE_MESSAGE,
    });
    expect(result.message).not.toContain("bkw_secret");
  });
});
