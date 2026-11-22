export const RATING_VALIDATION_MESSAGE =
  "Rating must be a whole number from 1 to 5, or empty for unrated.";
export const HISTORY_ID_VALIDATION_MESSAGE = "Choose a valid reading history entry.";

export type ParsedValue<T> =
  | { ok: true; value: T }
  | { ok: false; message: string };

export function parseWholeStarRating(raw: unknown): ParsedValue<number | null> {
  if (raw === null || raw === undefined) {
    return { ok: true, value: null };
  }
  if (typeof raw === "boolean") {
    return { ok: false, message: RATING_VALIDATION_MESSAGE };
  }
  if (typeof raw === "number") {
    if (Number.isInteger(raw) && raw >= 1 && raw <= 5) {
      return { ok: true, value: raw };
    }
    return { ok: false, message: RATING_VALIDATION_MESSAGE };
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (trimmed === "") {
      return { ok: true, value: null };
    }
    if (/^[1-5]$/.test(trimmed)) {
      return { ok: true, value: Number(trimmed) };
    }
    return { ok: false, message: RATING_VALIDATION_MESSAGE };
  }
  return { ok: false, message: RATING_VALIDATION_MESSAGE };
}

export function parseReadingHistoryId(raw: unknown): ParsedValue<number> {
  if (typeof raw === "boolean") {
    return { ok: false, message: HISTORY_ID_VALIDATION_MESSAGE };
  }
  if (typeof raw === "number") {
    if (Number.isSafeInteger(raw) && raw > 0) {
      return { ok: true, value: raw };
    }
    return { ok: false, message: HISTORY_ID_VALIDATION_MESSAGE };
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (/^[1-9]\d*$/.test(trimmed)) {
      const parsed = Number(trimmed);
      if (Number.isSafeInteger(parsed) && parsed > 0) {
        return { ok: true, value: parsed };
      }
    }
  }
  return { ok: false, message: HISTORY_ID_VALIDATION_MESSAGE };
}

export function effectiveBookRating(
  id: number,
  serverRating: number | null | undefined,
  overrides: Record<number, number | null>,
): number | null {
  if (Object.prototype.hasOwnProperty.call(overrides, id)) {
    return overrides[id];
  }
  return serverRating ?? null;
}

export function applyRatingOverrides<T extends { id: number; rating: number | null }>(
  items: readonly T[],
  overrides: Record<number, number | null>,
): T[] {
  return items.map((item) => {
    const rating = effectiveBookRating(item.id, item.rating, overrides);
    return rating === item.rating ? item : { ...item, rating };
  });
}

export function starButtonLabel(rating: number): string {
  return `${rating} ${rating === 1 ? "star" : "stars"}`;
}

export function formatRatingConfirmation(title: string, rating: number | null): string {
  if (rating === null) {
    return `Cleared rating for "${title}". Marked as unrated.`;
  }
  return `Rated "${title}" ${starButtonLabel(rating)}.`;
}
