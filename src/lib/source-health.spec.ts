import { describe, expect, it } from "vitest";
import { formatSourceScanStatus, sourceScanTone } from "./source-health.js";

describe("source scan health formatting", () => {
  it("formats healthy scans and un-scanned sources", () => {
    expect(formatSourceScanStatus(null, null)).toBe("Not scanned yet");
    expect(formatSourceScanStatus("2026-04-12T10:30:00Z", "ok:14")).toBe(
      "Scanned Apr 12",
    );
    expect(sourceScanTone(null, null)).toBe("idle");
    expect(sourceScanTone("2026-04-12T10:30:00Z", "ok:14")).toBe("healthy");
  });

  it("preserves the last good scan date alongside hostname errors", () => {
    expect(
      formatSourceScanStatus(
        "2026-04-12T10:30:00Z",
        "error:Client error '404 Not Found' for url 'https://books.example.test/feed'",
      ),
    ).toBe(
      "Last good scan Apr 12 · Needs attention: Client error '404 Not Found' for url 'https://books.example.test/feed'",
    );
    expect(formatSourceScanStatus(null, "error:Host could not be resolved")).toBe(
      "Needs attention: Host could not be resolved",
    );
    expect(
      sourceScanTone("2026-04-12T10:30:00Z", "error:Host could not be resolved"),
    ).toBe("error");
  });

  it("renders distinct warnings instead of Scanned on empty:0 and filtered:0", () => {
    const emptyWithGood = formatSourceScanStatus("2026-04-12T10:30:00Z", "empty:0");
    const filteredWithGood = formatSourceScanStatus(
      "2026-04-12T10:30:00Z",
      "filtered:0",
    );
    expect(emptyWithGood).toBe(
      "Last good scan Apr 12 · Warning: upstream feed returned 0 books (existing candidates kept)",
    );
    expect(filteredWithGood).toBe(
      "Last good scan Apr 12 · Warning: 0 books matched genre filters (existing candidates kept)",
    );
    expect(emptyWithGood).not.toBe(filteredWithGood);
    expect(emptyWithGood.startsWith("Scanned")).toBe(false);
    expect(filteredWithGood.startsWith("Scanned")).toBe(false);
    expect(formatSourceScanStatus(null, "empty:0")).toBe(
      "Warning: upstream feed returned 0 books (existing candidates kept)",
    );
    expect(formatSourceScanStatus(null, "filtered:0")).toBe(
      "Warning: 0 books matched genre filters (existing candidates kept)",
    );
    expect(sourceScanTone("2026-04-12T10:30:00Z", "empty:0")).toBe("warning");
    expect(sourceScanTone("2026-04-12T10:30:00Z", "filtered:0")).toBe("warning");
  });
});
