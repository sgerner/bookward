import { describe, expect, it } from "vitest";
import {
  HISTORY_ID_VALIDATION_MESSAGE,
  RATING_VALIDATION_MESSAGE,
  applyRatingOverrides,
  effectiveBookRating,
  formatRatingConfirmation,
  parseReadingHistoryId,
  parseWholeStarRating,
  starButtonLabel,
} from "./reading-history-ratings";

describe("reading history rating helpers", () => {
  it("accepts whole numbers 1 through 5 and empty/null values for unrated", () => {
    for (const rating of [1, 2, 3, 4, 5]) {
      expect(parseWholeStarRating(rating)).toEqual({ ok: true, value: rating });
      expect(parseWholeStarRating(String(rating))).toEqual({ ok: true, value: rating });
      expect(parseWholeStarRating(` ${rating} `)).toEqual({ ok: true, value: rating });
    }
    expect(parseWholeStarRating(null)).toEqual({ ok: true, value: null });
    expect(parseWholeStarRating(undefined)).toEqual({ ok: true, value: null });
    expect(parseWholeStarRating("")).toEqual({ ok: true, value: null });
    expect(parseWholeStarRating("   ")).toEqual({ ok: true, value: null });
  });

  it("rejects out-of-range, fractional, boolean, and malformed rating values", () => {
    for (const invalid of [0, -1, 6, 3.5, Number.NaN, Number.POSITIVE_INFINITY, true, false, "0", "6", "4.5", "five", {}, []]) {
      expect(parseWholeStarRating(invalid)).toEqual({
        ok: false,
        message: RATING_VALIDATION_MESSAGE,
      });
    }
  });

  it("validates positive integer reading history IDs", () => {
    expect(parseReadingHistoryId(1)).toEqual({ ok: true, value: 1 });
    expect(parseReadingHistoryId("42")).toEqual({ ok: true, value: 42 });
    for (const invalid of [0, -5, 1.5, true, null, undefined, "", "0", "abc", {}]) {
      expect(parseReadingHistoryId(invalid)).toEqual({
        ok: false,
        message: HISTORY_ID_VALIDATION_MESSAGE,
      });
    }
  });

  it("synchronizes rating overrides across History and Recent reads including null unrated", () => {
    const historyItems = [
      { id: 10, title: "Piranesi", rating: 4 },
      { id: 11, title: "Station Eleven", rating: null },
    ];
    const recentReads = [
      { id: 10, title: "Piranesi", rating: 4 },
      { id: 11, title: "Station Eleven", rating: null },
    ];
    const overrides: Record<number, number | null> = {
      10: null,
      11: 5,
    };

    expect(effectiveBookRating(10, 4, overrides)).toBeNull();
    expect(effectiveBookRating(11, null, overrides)).toBe(5);
    expect(effectiveBookRating(99, 3, overrides)).toBe(3);

    expect(applyRatingOverrides(historyItems, overrides)).toEqual([
      { id: 10, title: "Piranesi", rating: null },
      { id: 11, title: "Station Eleven", rating: 5 },
    ]);
    expect(applyRatingOverrides(recentReads, overrides)).toEqual([
      { id: 10, title: "Piranesi", rating: null },
      { id: 11, title: "Station Eleven", rating: 5 },
    ]);
  });

  it("formats accessible star button labels and confirmations", () => {
    expect(starButtonLabel(1)).toBe("1 star");
    expect(starButtonLabel(5)).toBe("5 stars");
    expect(formatRatingConfirmation("Piranesi", 5)).toBe('Rated "Piranesi" 5 stars.');
    expect(formatRatingConfirmation("Piranesi", 1)).toBe('Rated "Piranesi" 1 star.');
    expect(formatRatingConfirmation("Piranesi", null)).toBe(
      'Cleared rating for "Piranesi". Marked as unrated.',
    );
  });
});
