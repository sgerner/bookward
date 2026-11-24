import { describe, expect, it, vi } from "vitest";
import {
  HISTORY_ID_VALIDATION_MESSAGE,
  RATING_VALIDATION_MESSAGE,
  applyRatingOverrides,
  createReadingHistoryRatingController,
  effectiveBookRating,
  formatRatingConfirmation,
  parseReadingHistoryId,
  parseWholeStarRating,
  preserveRatingFocusAfterUpdate,
  starButtonLabel,
  type FocusDocumentLike,
  type FocusableElementLike,
} from "./reading-history-ratings";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("reading history rating helpers", () => {
  it("accepts whole numbers 1 through 5 and explicit null for unrated", () => {
    for (const rating of [1, 2, 3, 4, 5]) {
      expect(parseWholeStarRating(rating)).toEqual({ ok: true, value: rating });
      expect(parseWholeStarRating(String(rating))).toEqual({ ok: true, value: rating });
      expect(parseWholeStarRating(` ${rating} `)).toEqual({ ok: true, value: rating });
    }
    expect(parseWholeStarRating(null)).toEqual({ ok: true, value: null });
  });

  it("rejects missing (undefined), empty strings, out-of-range, fractional, boolean, and malformed rating values", () => {
    for (const invalid of [
      undefined,
      "",
      "   ",
      0,
      -1,
      6,
      3.5,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      true,
      false,
      "0",
      "6",
      "4.5",
      "five",
      {},
      [],
    ]) {
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

describe("reading history rating controller — per-id serialization & rollback", () => {
  it("serializes rapid edits per book id and coalesces intermediate ratings so stale PUTs never overwrite newer ones", async () => {
    const firstPut = deferred<number | null>();
    const secondPut = deferred<number | null>();
    const serverAppliedOrder: Array<{ id: number; rating: number | null }> = [];
    const saveCalls: Array<{ id: number; rating: number | null }> = [];
    const successes: Array<{ id: number; title: string; rating: number | null }> = [];
    let historyRowRating: number | null = null;

    const controller = createReadingHistoryRatingController({
      saveRating: vi.fn((id, rating) => {
        saveCalls.push({ id, rating });
        if (saveCalls.length === 1) {
          return firstPut.promise.then((res) => {
            serverAppliedOrder.push({ id, rating: res });
            return res;
          });
        }
        return secondPut.promise.then((res) => {
          serverAppliedOrder.push({ id, rating: res });
          return res;
        });
      }),
      onItemRatingChange: (_id, rating) => {
        historyRowRating = rating;
      },
      onCommittedSuccess: (res) => successes.push(res),
      doc: null,
    });

    const book = { id: 10, title: "Piranesi", rating: null };
    const p1 = controller.update(book, 2);
    const p2 = controller.update(book, 4);
    const p3 = controller.update(book, 5);

    // Optimistic UI immediately reflects latest value (5), while ONLY the first PUT (2) is in flight
    expect(historyRowRating).toBe(5);
    expect(controller.getState().overrides[10]).toBe(5);
    expect(controller.getState().pendingIds.has(10)).toBe(true);
    expect(saveCalls).toEqual([{ id: 10, rating: 2 }]);

    // Even when PUT(2) finishes slowly, PUT(5) only starts AFTER PUT(2) resolves, skipping superseded 4
    firstPut.resolve(2);
    await Promise.resolve();
    await Promise.resolve();

    expect(saveCalls).toEqual([
      { id: 10, rating: 2 },
      { id: 10, rating: 5 },
    ]);
    expect(controller.getState().overrides[10]).toBe(5);
    expect(controller.getState().pendingIds.has(10)).toBe(true);
    expect(successes).toHaveLength(0);

    secondPut.resolve(5);
    await Promise.all([p1, p2, p3]);

    expect(serverAppliedOrder).toEqual([
      { id: 10, rating: 2 },
      { id: 10, rating: 5 },
    ]);
    expect(historyRowRating).toBe(5);
    expect(controller.getState().overrides[10]).toBe(5);
    expect(controller.getState().committed[10]).toBe(5);
    expect(controller.getState().pendingIds.size).toBe(0);
    expect(successes).toEqual([{ id: 10, title: "Piranesi", rating: 5 }]);
  });

  it("allows concurrent edits on distinct book IDs without blocking each other", async () => {
    const putA = deferred<number | null>();
    const putB = deferred<number | null>();
    const inFlightCalls: Array<{ id: number; rating: number | null }> = [];

    const controller = createReadingHistoryRatingController({
      saveRating: vi.fn((id, rating) => {
        inFlightCalls.push({ id, rating });
        return id === 10 ? putA.promise : putB.promise;
      }),
      doc: null,
    });

    const pA = controller.update({ id: 10, title: "Book A", rating: null }, 5);
    const pB = controller.update({ id: 11, title: "Book B", rating: 3 }, null);

    expect(inFlightCalls).toEqual([
      { id: 10, rating: 5 },
      { id: 11, rating: null },
    ]);
    expect([...controller.getState().pendingIds]).toEqual([10, 11]);

    putB.resolve(null);
    await pB;
    expect(controller.getState().pendingIds.has(11)).toBe(false);
    expect(controller.getState().pendingIds.has(10)).toBe(true);

    putA.resolve(5);
    await pA;
    expect(controller.getState().pendingIds.size).toBe(0);
  });

  it("rolls optimistic rating changes back to the last server-confirmed rating on error", async () => {
    const rollbacks: Array<{
      id: number;
      revertedRating: number | null;
      serverMutatedBeforeFailure: boolean;
      message: string;
    }> = [];
    const itemRatings = new Map<number, number | null>([
      [10, null],
      [11, 4],
    ]);

    const controller = createReadingHistoryRatingController({
      saveRating: vi.fn().mockRejectedValue(new Error("Service unavailable")),
      onItemRatingChange: (id, rating) => {
        itemRatings.set(id, rating);
      },
      onRollbackError: ({ id, revertedRating, serverMutatedBeforeFailure, error }) => {
        rollbacks.push({
          id,
          revertedRating,
          serverMutatedBeforeFailure,
          message: error instanceof Error ? error.message : String(error),
        });
      },
      doc: null,
    });

    // 1. Failing null -> 5 rolls back to null
    await controller.update({ id: 10, title: "Station Eleven", rating: null }, 5);
    expect(itemRatings.get(10)).toBeNull();
    expect(controller.getState().overrides[10]).toBeNull();
    expect(controller.getState().pendingIds.size).toBe(0);

    // 2. Failing 4 -> null (clear) rolls back to 4
    await controller.update({ id: 11, title: "Piranesi", rating: 4 }, null);
    expect(itemRatings.get(11)).toBe(4);
    expect(controller.getState().overrides[11]).toBe(4);
    expect(rollbacks).toEqual([
      {
        id: 10,
        revertedRating: null,
        serverMutatedBeforeFailure: false,
        message: "Service unavailable",
      },
      {
        id: 11,
        revertedRating: 4,
        serverMutatedBeforeFailure: false,
        message: "Service unavailable",
      },
    ]);
  });

  it("rolls back to the newly committed rating when an initial PUT succeeds and a queued follow-up PUT fails", async () => {
    const firstPut = deferred<number | null>();
    const secondPut = deferred<number | null>();
    let callCount = 0;
    const rollbacks: Array<{ revertedRating: number | null; serverMutatedBeforeFailure: boolean }> =
      [];

    const controller = createReadingHistoryRatingController({
      saveRating: vi.fn(() => {
        callCount += 1;
        return callCount === 1 ? firstPut.promise : secondPut.promise;
      }),
      onRollbackError: ({ revertedRating, serverMutatedBeforeFailure }) => {
        rollbacks.push({ revertedRating, serverMutatedBeforeFailure });
      },
      doc: null,
    });

    const book = { id: 10, title: "Piranesi", rating: null };
    const p1 = controller.update(book, 2);
    const p2 = controller.update(book, 5);

    firstPut.resolve(2);
    await Promise.resolve();
    await Promise.resolve();

    secondPut.reject(new Error("Engine timeout"));
    await Promise.all([p1, p2]);

    // Server committed 2 before 5 failed, so UI rolls back to 2 (not stale null)
    expect(controller.getState().overrides[10]).toBe(2);
    expect(controller.getState().committed[10]).toBe(2);
    expect(rollbacks).toEqual([
      { revertedRating: 2, serverMutatedBeforeFailure: true },
    ]);
  });
});

describe("reading history rating focus handling", () => {
  function createFocusTarget(isConnected = true) {
    return {
      isConnected,
      focus: vi.fn<() => void>(),
    };
  }

  it("moves focus to the row's 1-star radio when Clear rating unmounts while keyboard-focused", async () => {
    const clearButton = createFocusTarget(true);
    const firstStar = createFocusTarget(true);
    const doc: FocusDocumentLike = {
      activeElement: clearButton,
      body: {},
      querySelector: (selector) =>
        selector === '[data-rating-star="history-10-1"]' ? firstStar : null,
    };

    const controller = createReadingHistoryRatingController({
      saveRating: vi.fn().mockResolvedValue(null),
      afterDomUpdate: async () => {
        // Simulate Svelte unmounting Clear rating button -> browser drops focus to body
        clearButton.isConnected = false;
        doc.activeElement = doc.body;
      },
      doc,
    });

    await controller.update(
      { id: 10, title: "Piranesi", rating: 4 },
      null,
      "history",
      clearButton,
      true,
    );

    expect(firstStar.focus).toHaveBeenCalledTimes(1);
  });

  it("falls back to the next visible row's active radio or rating filter when an active filter unmounts the edited card", () => {
    const body = {};
    const nextRowRadio = createFocusTarget(true);
    const filterSelect = createFocusTarget(true);

    const docWithNextRow: FocusDocumentLike = {
      activeElement: body,
      body,
      querySelector: (selector) => {
        if (selector === '[data-rating-group^="history-"] [role="radio"][tabindex="0"]') {
          return nextRowRadio;
        }
        if (selector === '[aria-label="Filter read books by rating"]') {
          return filterSelect;
        }
        return null;
      },
    };

    preserveRatingFocusAfterUpdate({
      doc: docWithNextRow,
      surface: "history",
      itemId: 10,
      hadFocus: true,
      clearedViaButton: false,
    });
    expect(nextRowRadio.focus).toHaveBeenCalledTimes(1);
    expect(filterSelect.focus).not.toHaveBeenCalled();

    const docEmptyList: FocusDocumentLike = {
      activeElement: body,
      body,
      querySelector: (selector) =>
        selector === '[aria-label="Filter read books by rating"]' ? filterSelect : null,
    };

    preserveRatingFocusAfterUpdate({
      doc: docEmptyList,
      surface: "history",
      itemId: 10,
      hadFocus: true,
      clearedViaButton: false,
    });
    expect(filterSelect.focus).toHaveBeenCalledTimes(1);
  });

  it("preserves focus in place on mounted radios and never steals focus when trigger lacked focus", () => {
    const starFour = createFocusTarget(true);
    const starOne = createFocusTarget(true);
    const otherFocusedElement = createFocusTarget(true);

    // Mounted radio retains focus
    preserveRatingFocusAfterUpdate({
      doc: {
        activeElement: starFour,
        body: {},
        querySelector: () => starOne,
      },
      surface: "recent",
      itemId: 10,
      hadFocus: true,
      clearedViaButton: false,
    });
    expect(starOne.focus).not.toHaveBeenCalled();

    // Unfocused pointer interaction does not hijack focus
    preserveRatingFocusAfterUpdate({
      doc: {
        activeElement: otherFocusedElement,
        body: {},
        querySelector: () => starOne,
      },
      surface: "recent",
      itemId: 10,
      hadFocus: false,
      clearedViaButton: true,
    });
    expect(starOne.focus).not.toHaveBeenCalled();
  });
});
