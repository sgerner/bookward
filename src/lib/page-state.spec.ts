import { describe, expect, it } from "vitest";
import {
  READING_HISTORY_SCORE_BATCH_SIZE,
  applyReadingHistoryScores,
  buildViewUrl,
  isSameNavigationUrl,
  mergeLoadedBooks,
  readUrlViewState,
  reconcileBookMutation,
  reconcilePagedBooks,
  runReadingHistoryScoreBatches,
  sortReadingHistory,
  type ReadingHistoryScorePatch,
} from "./page-state";

type TestBook = {
  id: number;
  title: string;
  status: string;
};

const book = (id: number, status = "recommended"): TestBook => ({
  id,
  title: `Book ${id}`,
  status,
});

describe("URL view state helpers", () => {
  it("synchronizes view, History sub-filter, and digest mode from URL search params", () => {
    expect(readUrlViewState(new URL("http://bookward.test/"))).toEqual({
      view: "discover",
      decisionFilter: "all",
      digestMode: false,
    });

    expect(
      readUrlViewState(
        new URL("http://bookward.test/?view=decisions&filter=read"),
      ),
    ).toEqual({
      view: "decisions",
      decisionFilter: "read",
      digestMode: false,
    });

    expect(
      readUrlViewState(
        new URL("http://bookward.test/?view=decisions&filter=maybe_later"),
      ),
    ).toEqual({
      view: "decisions",
      decisionFilter: "maybe_later",
      digestMode: false,
    });

    expect(
      readUrlViewState(
        new URL("http://bookward.test/?view=discover&digest=1"),
      ),
    ).toEqual({
      view: "discover",
      decisionFilter: "all",
      digestMode: true,
    });
  });

  it("drops stale digest_period mode when server digest review was not found", () => {
    const staleUrl = new URL(
      "http://bookward.test/?view=discover&digest=1&digest_period=2026-W01",
    );
    expect(readUrlViewState(staleUrl, false)).toEqual({
      view: "discover",
      decisionFilter: "all",
      digestMode: false,
    });
    expect(readUrlViewState(staleUrl, true)).toEqual({
      view: "discover",
      decisionFilter: "all",
      digestMode: true,
    });
  });

  it("builds canonical URLs for home, tabs, History sub-filters, and digest review", () => {
    const start = new URL(
      "http://bookward.test/?view=decisions&filter=read",
    );
    const home = buildViewUrl(start, { view: "discover" });
    expect(home.pathname + home.search).toBe("/");

    const readHistory = buildViewUrl(new URL("http://bookward.test/"), {
      view: "decisions",
      decisionFilter: "read",
    });
    expect(readHistory.pathname + readHistory.search).toBe(
      "/?view=decisions&filter=read",
    );

    const allHistory = buildViewUrl(readHistory, {
      view: "decisions",
      decisionFilter: "all",
    });
    expect(allHistory.pathname + allHistory.search).toBe("/?view=decisions");

    const digest = buildViewUrl(
      new URL("http://bookward.test/?digest_period=2026-W40"),
      {
        view: "discover",
        digestMode: true,
        clearDigestPeriod: false,
      },
    );
    expect(digest.pathname + digest.search).toBe(
      "/?digest_period=2026-W40&view=discover",
    );
  });

  it("detects identical navigation URLs so active tabs do not push duplicate history entries", () => {
    const current = new URL("http://bookward.test/?view=saved");
    const duplicate = buildViewUrl(current, { view: "saved" });
    expect(isSameNavigationUrl(current, duplicate)).toBe(true);

    const next = buildViewUrl(current, {
      view: "decisions",
      decisionFilter: "read",
    });
    expect(isSameNavigationUrl(current, next)).toBe(false);
  });
});

describe("paged card reconciliation past item 24", () => {
  it("preserves loaded Discover items past position 24 when curating a single card", () => {
    const initialServerBooks = Array.from({ length: 24 }, (_, idx) =>
      book(idx + 1, "recommended"),
    );
    const pagedDiscoverBooks = Array.from({ length: 8 }, (_, idx) =>
      book(idx + 25, "recommended"),
    );

    // Reader shortlists item #3; server overview shifts item #25 into the 24-item slice.
    const refreshedServerBooks = [
      ...initialServerBooks.filter((item) => item.id !== 3),
      book(25, "recommended"),
    ];
    const reconciled = reconcileBookMutation({
      serverBooks: refreshedServerBooks,
      serverDecisions: [],
      additionalDiscoverBooks: pagedDiscoverBooks,
      additionalDecisionBooks: [],
      id: 3,
      nextStatus: "saved",
      snapshot: book(3, "recommended"),
    });

    expect(reconciled.additionalDiscoverBooks.map((item) => item.id)).toEqual([
      26, 27, 28, 29, 30, 31, 32,
    ]);

    const merged = mergeLoadedBooks(
      refreshedServerBooks,
      [],
      reconciled.additionalDiscoverBooks,
      reconciled.additionalDecisionBooks,
    );
    expect(
      merged
        .filter((item) => item.status === "recommended")
        .map((item) => item.id),
    ).toEqual([
      1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
      22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32,
    ]);
  });

  it("moves a paged card past item 24 across Discover, History, Undo, and Mark Read without resetting pagination", () => {
    const serverBooks = Array.from({ length: 24 }, (_, idx) =>
      book(idx + 1, "recommended"),
    );
    const serverDecisions = Array.from({ length: 24 }, (_, idx) =>
      book(101 + idx, "rejected"),
    );
    const additionalDiscover = [
      book(25, "recommended"),
      book(26, "recommended"),
      book(27, "recommended"),
    ];
    const additionalDecisions = [
      book(125, "rejected"),
      book(126, "maybe_later"),
    ];

    // Pass on paged Discover item #26: removed from Discover page 2, preserved #25 and #27.
    const afterPass = reconcileBookMutation({
      serverBooks,
      serverDecisions,
      additionalDiscoverBooks: additionalDiscover,
      additionalDecisionBooks: additionalDecisions,
      id: 26,
      nextStatus: "rejected",
      snapshot: book(26, "recommended"),
    });
    expect(afterPass.additionalDiscoverBooks.map((item) => item.id)).toEqual([
      25, 27,
    ]);
    expect(afterPass.additionalDecisionBooks.map((item) => item.id)).toEqual([
      125, 126, 26,
    ]);

    // Undo restores item #26 back to its exact index (1) in additionalDiscoverBooks.
    const afterUndo = reconcileBookMutation({
      serverBooks,
      serverDecisions,
      additionalDiscoverBooks: afterPass.additionalDiscoverBooks,
      additionalDecisionBooks: afterPass.additionalDecisionBooks,
      id: 26,
      nextStatus: "recommended",
      snapshot: book(26, "recommended"),
      restoreDiscoverIndex: 1,
    });
    expect(afterUndo.additionalDiscoverBooks.map((item) => item.id)).toEqual([
      25, 26, 27,
    ]);
    expect(afterUndo.additionalDecisionBooks.map((item) => item.id)).toEqual([
      125, 126,
    ]);

    // Mark Read on paged History item #126 removes only #126 while keeping #125 and Discover items.
    const afterMarkRead = reconcileBookMutation({
      serverBooks,
      serverDecisions,
      additionalDiscoverBooks: afterUndo.additionalDiscoverBooks,
      additionalDecisionBooks: afterUndo.additionalDecisionBooks,
      id: 126,
      nextStatus: "read",
      snapshot: book(126, "maybe_later"),
    });
    expect(
      afterMarkRead.additionalDiscoverBooks.map((item) => item.id),
    ).toEqual([25, 26, 27]);
    expect(
      afterMarkRead.additionalDecisionBooks.map((item) => item.id),
    ).toEqual([125]);
  });

  it("deduplicates merged books by id before effects run when server slices shift", () => {
    const serverBooks = [book(1, "recommended"), book(25, "recommended")];
    const additionalDiscover = [book(25, "recommended"), book(26, "recommended")];
    const merged = mergeLoadedBooks(
      serverBooks,
      [],
      additionalDiscover,
      [],
      { 26: "saved" },
    );
    expect(merged).toEqual([
      book(1, "recommended"),
      book(25, "recommended"),
      book(26, "saved"),
    ]);

    const reconciled = reconcilePagedBooks(
      serverBooks,
      [],
      additionalDiscover,
      [],
    );
    expect(reconciled.additionalDiscoverBooks).toEqual([
      book(26, "recommended"),
    ]);
  });
});

describe("batched reading-history score buffering and sorting", () => {
  it("uses a bounded 200-row score batch size", () => {
    expect(READING_HISTORY_SCORE_BATCH_SIZE).toBe(200);
  });

  it("keeps stable chronological order while scores are buffered, then reorders once when scores commit", () => {
    const initialItems = [
      {
        id: 1,
        title: "Zulu",
        rating: 4,
        read_at: "2026-03-01",
        created_at: "2026-03-01T00:00:00Z",
        algorithm_score: null,
        algorithm_explanation: [] as string[],
        metadata_confidence: null,
      },
      {
        id: 2,
        title: "Alpha",
        rating: 5,
        read_at: "2026-02-01",
        created_at: "2026-02-01T00:00:00Z",
        algorithm_score: null,
        algorithm_explanation: [] as string[],
        metadata_confidence: null,
      },
      {
        id: 3,
        title: "Middle",
        rating: 3,
        read_at: "2026-01-01",
        created_at: "2026-01-01T00:00:00Z",
        algorithm_score: null,
        algorithm_explanation: [] as string[],
        metadata_confidence: null,
      },
    ];

    // While batches are still in flight (scoresPending=true), score_high preserves chronological order
    // instead of reshuffling cards alphabetically first.
    expect(
      sortReadingHistory(initialItems, "score_high", true).map((item) => item.id),
    ).toEqual([1, 2, 3]);

    // Buffer two batches into a Map before applying to readingHistory in a single pass.
    const buffered = new Map<number, ReadingHistoryScorePatch>();
    buffered.set(1, {
      id: 1,
      algorithm_score: 68.5,
      algorithm_explanation: ["Solid match"],
      metadata_confidence: 1,
    });
    buffered.set(2, {
      id: 2,
      algorithm_score: 94.2,
      algorithm_explanation: ["Top match"],
      metadata_confidence: 1,
    });
    buffered.set(3, {
      id: 3,
      algorithm_score: 81.0,
      algorithm_explanation: ["Good match"],
      metadata_confidence: 0.9,
    });

    const scored = applyReadingHistoryScores(initialItems, buffered);
    expect(
      sortReadingHistory(scored, "score_high", false).map((item) => item.id),
    ).toEqual([2, 3, 1]);
    expect(
      sortReadingHistory(scored, "score_low", false).map((item) => item.id),
    ).toEqual([1, 3, 2]);
  });

  it("preserves readingHistoryScoreOffset and buffered scores on partial batch failure, then retries from the failed offset", async () => {
    const requestedRanges: Array<[number, number]> = [];
    let failSecondBatchOnce = true;

    const makePatch = (id: number): ReadingHistoryScorePatch => ({
      id,
      algorithm_score: 50 + (id % 40),
      algorithm_explanation: [`Score for ${id}`],
      metadata_confidence: 1,
    });

    let readingHistoryScoreOffset = 0;
    let bufferedReadingHistoryScores = new Map<
      number,
      ReadingHistoryScorePatch
    >();

    const firstAttempt = await runReadingHistoryScoreBatches({
      total: 430,
      startOffset: readingHistoryScoreOffset,
      initialBuffer: bufferedReadingHistoryScores,
      fetchBatch: async (offset, limit) => {
        requestedRanges.push([offset, limit]);
        if (offset === 200 && failSecondBatchOnce) {
          failSecondBatchOnce = false;
          return {
            ok: false,
            status: 502,
            message: "Second batch timed out.",
          };
        }
        const count = Math.min(limit, 430 - offset);
        return {
          ok: true,
          status: 200,
          scores: Array.from({ length: count }, (_, idx) =>
            makePatch(offset + idx + 1),
          ),
        };
      },
      onBatch: ({ nextOffset, buffered }) => {
        readingHistoryScoreOffset = nextOffset;
        bufferedReadingHistoryScores = buffered;
      },
    });

    expect(firstAttempt.status).toBe("error");
    if (firstAttempt.status !== "error") throw new Error("Expected error");
    expect(firstAttempt.message).toBe("Second batch timed out.");
    expect(readingHistoryScoreOffset).toBe(200);
    expect(bufferedReadingHistoryScores.size).toBe(200);
    expect(requestedRanges).toEqual([
      [0, 200],
      [200, 200],
    ]);

    // Retrying resumes from readingHistoryScoreOffset (200) without re-fetching offset 0.
    const retryAttempt = await runReadingHistoryScoreBatches({
      total: 430,
      startOffset: readingHistoryScoreOffset,
      initialBuffer: bufferedReadingHistoryScores,
      fetchBatch: async (offset, limit) => {
        requestedRanges.push([offset, limit]);
        const count = Math.min(limit, 430 - offset);
        return {
          ok: true,
          status: 200,
          scores: Array.from({ length: count }, (_, idx) =>
            makePatch(offset + idx + 1),
          ),
        };
      },
      onBatch: ({ nextOffset, buffered }) => {
        readingHistoryScoreOffset = nextOffset;
        bufferedReadingHistoryScores = buffered;
      },
    });

    expect(retryAttempt.status).toBe("ready");
    if (retryAttempt.status !== "ready") throw new Error("Expected ready");
    expect(readingHistoryScoreOffset).toBe(600);
    expect(retryAttempt.buffered.size).toBe(430);
    expect(requestedRanges).toEqual([
      [0, 200],
      [200, 200],
      [200, 200],
      [400, 200],
    ]);
  });

  it("steps down from 200-row batches to 20-row batches if the server enforces the legacy 20-row limit", async () => {
    const requestedRanges: Array<[number, number]> = [];
    const outcome = await runReadingHistoryScoreBatches({
      total: 35,
      startOffset: 0,
      initialBuffer: new Map(),
      fetchBatch: async (offset, limit) => {
        requestedRanges.push([offset, limit]);
        if (limit > 20) {
          return {
            ok: false,
            status: 400,
            message: "Invalid reading history score range.",
          };
        }
        const count = Math.min(limit, 35 - offset);
        return {
          ok: true,
          status: 200,
          scores: Array.from({ length: count }, (_, idx) => ({
            id: offset + idx + 1,
            algorithm_score: 75,
            algorithm_explanation: [],
            metadata_confidence: 1,
          })),
        };
      },
    });

    expect(outcome.status).toBe("ready");
    if (outcome.status !== "ready") throw new Error("Expected ready");
    expect(outcome.buffered.size).toBe(35);
    expect(requestedRanges).toEqual([
      [0, 200],
      [0, 20],
      [20, 20],
    ]);
  });
});
