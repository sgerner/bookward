import { describe, expect, it } from "vitest";
import { formatReadingSourceLabel } from "./reading-sources";

describe("formatReadingSourceLabel", () => {
  it("distinguishes StoryGraph and Goodreads CSV exports", () => {
    expect(formatReadingSourceLabel("storygraph_csv")).toBe(
      "StoryGraph CSV export",
    );
    expect(formatReadingSourceLabel("goodreads_csv")).toBe(
      "Goodreads CSV export",
    );
    expect(formatReadingSourceLabel("goodreads_rss")).toBe(
      "Goodreads read shelf RSS",
    );
    expect(formatReadingSourceLabel("manual")).toBe("Added in Bookward");
    expect(formatReadingSourceLabel("demo")).toBe("Demo data");
  });

  it("falls back cleanly for unknown or empty sources", () => {
    expect(formatReadingSourceLabel("custom_import")).toBe("custom_import");
    expect(formatReadingSourceLabel("")).toBe("Unknown source");
  });

  it("formats manual past reads added in Bookward", () => {
    expect(formatReadingSourceLabel("manual_history")).toBe(
      "Past read added in Bookward",
    );
  });
});
