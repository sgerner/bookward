export type View = "discover" | "saved" | "decisions" | "sources" | "settings";
export type DecisionFilter = "all" | "maybe_later" | "rejected" | "read";
export type ReadingHistorySort =
  | "recent"
  | "oldest"
  | "score_high"
  | "score_low"
  | "rating_high"
  | "title";

export type UrlViewState = {
  view: View;
  decisionFilter: DecisionFilter;
  digestMode: boolean;
};

export type UrlLike = {
  href: string;
  pathname: string;
  search: string;
  hash: string;
  searchParams: {
    get(name: string): string | null;
    has(name: string): boolean;
  };
};

export type ReadingHistoryScorePatch = {
  id: number;
  algorithm_score: number | null;
  algorithm_explanation: string[];
  metadata_confidence: number | null;
};

export type ReadingHistoryScoreBatchResponse = {
  ok: boolean;
  status: number;
  scores?: ReadingHistoryScorePatch[];
  message?: string;
};

export type ReadingHistoryScoreBatchOutcome =
  | { status: "stale" }
  | {
      status: "ready";
      nextOffset: number;
      buffered: Map<number, ReadingHistoryScorePatch>;
    }
  | {
      status: "error";
      message: string;
      nextOffset: number;
      buffered: Map<number, ReadingHistoryScorePatch>;
    };

export const READING_HISTORY_SCORE_BATCH_SIZE = 200;
const LEGACY_READING_HISTORY_SCORE_BATCH_SIZE = 20;

export async function runReadingHistoryScoreBatches(params: {
  total: number;
  startOffset: number;
  initialBuffer: ReadonlyMap<number, ReadingHistoryScorePatch>;
  batchSize?: number;
  isCurrent?: () => boolean;
  fetchBatch: (
    offset: number,
    limit: number,
  ) => Promise<ReadingHistoryScoreBatchResponse>;
  onBatch?: (progress: {
    nextOffset: number;
    buffered: Map<number, ReadingHistoryScorePatch>;
  }) => void;
}): Promise<ReadingHistoryScoreBatchOutcome> {
  let currentOffset = params.startOffset;
  let activeBatchSize = params.batchSize ?? READING_HISTORY_SCORE_BATCH_SIZE;
  let buffered = new Map(params.initialBuffer);

  while (currentOffset < params.total) {
    let response: ReadingHistoryScoreBatchResponse;
    try {
      response = await params.fetchBatch(currentOffset, activeBatchSize);
    } catch (error) {
      if (params.isCurrent && !params.isCurrent()) {
        return { status: "stale" };
      }
      return {
        status: "error",
        message:
          error instanceof Error
            ? error.message
            : "Reading history scores could not be loaded.",
        nextOffset: currentOffset,
        buffered,
      };
    }

    if (params.isCurrent && !params.isCurrent()) {
      return { status: "stale" };
    }

    if (
      !response.ok &&
      response.status === 400 &&
      activeBatchSize > LEGACY_READING_HISTORY_SCORE_BATCH_SIZE
    ) {
      activeBatchSize = LEGACY_READING_HISTORY_SCORE_BATCH_SIZE;
      continue;
    }

    if (!response.ok || !Array.isArray(response.scores)) {
      return {
        status: "error",
        message:
          response.message || "Reading history scores could not be loaded.",
        nextOffset: currentOffset,
        buffered,
      };
    }

    const nextBuffer = new Map(buffered);
    for (const score of response.scores) {
      nextBuffer.set(score.id, score);
    }
    buffered = nextBuffer;
    currentOffset += activeBatchSize;
    params.onBatch?.({ nextOffset: currentOffset, buffered });
  }

  if (params.isCurrent && !params.isCurrent()) {
    return { status: "stale" };
  }

  return {
    status: "ready",
    nextOffset: currentOffset,
    buffered,
  };
}

const VALID_VIEWS = new Set<View>([
  "discover",
  "saved",
  "decisions",
  "sources",
  "settings",
]);
const VALID_DECISION_FILTERS = new Set<DecisionFilter>([
  "all",
  "maybe_later",
  "rejected",
  "read",
]);

export function readView(value: string | null): View {
  return value && VALID_VIEWS.has(value as View) ? (value as View) : "discover";
}

export function readDecisionFilter(value: string | null): DecisionFilter {
  return value && VALID_DECISION_FILTERS.has(value as DecisionFilter)
    ? (value as DecisionFilter)
    : "all";
}

export function readUrlViewState(
  url: UrlLike,
  digestReviewRequested = false,
): UrlViewState {
  const view = readView(url.searchParams.get("view"));
  const decisionFilter =
    view === "decisions"
      ? readDecisionFilter(url.searchParams.get("filter"))
      : "all";
  const digestRequested =
    view === "discover" && url.searchParams.get("digest") === "1";
  const staleDigestPeriod =
    url.searchParams.has("digest_period") && !digestReviewRequested;
  return {
    view,
    decisionFilter,
    digestMode: digestRequested && !staleDigestPeriod,
  };
}

export function buildViewUrl(
  currentUrl: UrlLike,
  next: {
    view: View;
    decisionFilter?: DecisionFilter;
    digestMode?: boolean;
    clearDigestPeriod?: boolean;
  },
): URL {
  const url = new URL(currentUrl.href);
  const digestMode = next.view === "discover" && Boolean(next.digestMode);
  if (next.view === "discover" && !digestMode) {
    url.searchParams.delete("view");
  } else {
    url.searchParams.set("view", next.view);
  }

  if (next.view !== "discover" || !digestMode) {
    url.searchParams.delete("digest");
    if (next.view !== "discover" || next.clearDigestPeriod !== false) {
      url.searchParams.delete("digest_period");
    }
  }

  const filter = next.view === "decisions" ? (next.decisionFilter ?? "all") : "all";
  if (filter === "all") {
    url.searchParams.delete("filter");
  } else {
    url.searchParams.set("filter", filter);
  }
  return url;
}

export function isSameNavigationUrl(left: UrlLike, right: UrlLike): boolean {
  return (
    left.pathname === right.pathname &&
    left.search === right.search &&
    left.hash === right.hash
  );
}

export type UrlNavigationControllerOptions = {
  getState: () => UrlViewState;
  setState: (patch: Partial<UrlViewState>) => void;
  getDigestReviewRequested: () => boolean;
  getCurrentLocationUrl: () => URL | null;
  pushUrl: (url: URL) => void;
  scrollToTop?: () => void;
  onViewEvaluated?: (view: View) => void;
  onResetDecisionPagination?: () => void;
  onClearDecisionsError?: () => void;
  onExitDigestMode?: () => void;
  onRequestReadingHistoryLoad?: () => void;
};

export function createUrlNavigationController(
  options: UrlNavigationControllerOptions,
) {
  return {
    syncFromUrl(url: UrlLike) {
      const nextUrlState = readUrlViewState(
        url,
        options.getDigestReviewRequested(),
      );
      const previous = options.getState();

      options.onViewEvaluated?.(nextUrlState.view);
      if (previous.view !== nextUrlState.view) {
        options.setState({ view: nextUrlState.view });
        options.scrollToTop?.();
      }
      if (previous.decisionFilter !== nextUrlState.decisionFilter) {
        options.setState({ decisionFilter: nextUrlState.decisionFilter });
        options.onResetDecisionPagination?.();
        options.onClearDecisionsError?.();
      }
      if (previous.digestMode !== nextUrlState.digestMode) {
        options.setState({ digestMode: nextUrlState.digestMode });
        if (!nextUrlState.digestMode) {
          options.onExitDigestMode?.();
        }
      }
    },

    selectDecisionFilter(nextFilter: DecisionFilter) {
      const current = options.getState();
      const filterChanged = current.decisionFilter !== nextFilter;
      options.setState({ decisionFilter: nextFilter });
      if (filterChanged) {
        options.onResetDecisionPagination?.();
      }
      options.onClearDecisionsError?.();
      if (nextFilter === "read") {
        options.onRequestReadingHistoryLoad?.();
      }
      const currentUrl = options.getCurrentLocationUrl();
      if (currentUrl && current.view === "decisions") {
        const nextUrl = buildViewUrl(currentUrl, {
          view: "decisions",
          decisionFilter: nextFilter,
        });
        if (!isSameNavigationUrl(currentUrl, nextUrl)) {
          options.pushUrl(nextUrl);
        }
      }
    },

    go(view: View, nextDecisionFilter?: DecisionFilter) {
      const current = options.getState();
      const targetFilter: DecisionFilter =
        view === "decisions"
          ? (nextDecisionFilter ??
            (current.view === "decisions" ? current.decisionFilter : "all"))
          : "all";
      if (
        view === current.view &&
        (nextDecisionFilter === undefined ||
          targetFilter === current.decisionFilter)
      ) {
        return;
      }
      const viewChanged = view !== current.view;
      const keepDigestMode = view === "discover" && current.digestMode;
      const exitedDigestMode = view !== "discover" && current.digestMode;

      options.onViewEvaluated?.(view);
      options.setState({
        view,
        decisionFilter: targetFilter,
        digestMode: keepDigestMode,
      });
      if (exitedDigestMode) {
        options.onExitDigestMode?.();
      }
      if (view === "decisions") {
        options.onResetDecisionPagination?.();
        if (targetFilter === "read") {
          options.onRequestReadingHistoryLoad?.();
        }
      }
      const currentUrl = options.getCurrentLocationUrl();
      if (currentUrl) {
        const nextUrl = buildViewUrl(currentUrl, {
          view,
          decisionFilter: targetFilter,
          digestMode: keepDigestMode,
          clearDigestPeriod: !keepDigestMode,
        });
        if (!isSameNavigationUrl(currentUrl, nextUrl)) {
          options.pushUrl(nextUrl);
        }
        if (viewChanged) {
          options.scrollToTop?.();
        }
      }
    },

    goHome() {
      const current = options.getState();
      options.onViewEvaluated?.("discover");
      options.setState({
        view: "discover",
        decisionFilter: "all",
        digestMode: false,
      });
      if (current.digestMode) {
        options.onExitDigestMode?.();
      }
      const currentUrl = options.getCurrentLocationUrl();
      if (currentUrl) {
        const nextUrl = new URL("/", currentUrl.origin);
        if (!isSameNavigationUrl(currentUrl, nextUrl)) {
          options.pushUrl(nextUrl);
        }
        options.scrollToTop?.();
      }
    },
  };
}

export function mergeLoadedBooks<T extends { id: number; status: string }>(
  serverBooks: readonly T[],
  serverDecisions: readonly T[],
  additionalDiscoverBooks: readonly T[],
  additionalDecisionBooks: readonly T[],
  statusOverrides: Readonly<Record<number, string>> = {},
): T[] {
  const seen = new Set<number>();
  const merged: T[] = [];
  for (const source of [
    serverBooks,
    serverDecisions,
    additionalDiscoverBooks,
    additionalDecisionBooks,
  ]) {
    for (const book of source) {
      if (seen.has(book.id)) continue;
      seen.add(book.id);
      const override = statusOverrides[book.id];
      merged.push(override === undefined ? book : { ...book, status: override });
    }
  }
  return merged;
}

export function reconcilePagedBooks<T extends { id: number; status: string }>(
  serverBooks: readonly T[],
  serverDecisions: readonly T[],
  additionalDiscoverBooks: readonly T[],
  additionalDecisionBooks: readonly T[],
): { additionalDiscoverBooks: T[]; additionalDecisionBooks: T[] } {
  const serverIds = new Set<number>();
  for (const book of serverBooks) serverIds.add(book.id);
  for (const book of serverDecisions) serverIds.add(book.id);

  const seenDiscover = new Set<number>();
  const nextDiscover: T[] = [];
  for (const book of additionalDiscoverBooks) {
    if (serverIds.has(book.id) || seenDiscover.has(book.id)) continue;
    if (book.status !== "recommended") continue;
    seenDiscover.add(book.id);
    nextDiscover.push(book);
  }

  const seenDecisions = new Set<number>();
  const nextDecisions: T[] = [];
  for (const book of additionalDecisionBooks) {
    if (
      serverIds.has(book.id) ||
      seenDiscover.has(book.id) ||
      seenDecisions.has(book.id)
    ) {
      continue;
    }
    if (book.status !== "rejected" && book.status !== "maybe_later") continue;
    seenDecisions.add(book.id);
    nextDecisions.push(book);
  }

  return {
    additionalDiscoverBooks: nextDiscover,
    additionalDecisionBooks: nextDecisions,
  };
}

export function reconcileBookMutation<T extends { id: number; status: string }>(params: {
  serverBooks: readonly T[];
  serverDecisions: readonly T[];
  additionalDiscoverBooks: readonly T[];
  additionalDecisionBooks: readonly T[];
  id: number;
  nextStatus: string;
  snapshot?: T;
  restoreDiscoverIndex?: number;
  restoreDecisionIndex?: number;
}): { additionalDiscoverBooks: T[]; additionalDecisionBooks: T[] } {
  const {
    serverBooks,
    serverDecisions,
    id,
    nextStatus,
    snapshot,
    restoreDiscoverIndex,
    restoreDecisionIndex,
  } = params;
  const reconciled = reconcilePagedBooks(
    serverBooks,
    serverDecisions,
    params.additionalDiscoverBooks.filter((book) => book.id !== id),
    params.additionalDecisionBooks.filter((book) => book.id !== id),
  );

  const inServerSlice =
    serverBooks.some((book) => book.id === id) ||
    serverDecisions.some((book) => book.id === id);
  if (inServerSlice || !snapshot) {
    return reconciled;
  }

  const updated = { ...snapshot, status: nextStatus };
  if (nextStatus === "recommended") {
    const nextDiscover = [...reconciled.additionalDiscoverBooks];
    if (
      restoreDiscoverIndex !== undefined &&
      restoreDiscoverIndex >= 0 &&
      restoreDiscoverIndex <= nextDiscover.length
    ) {
      nextDiscover.splice(restoreDiscoverIndex, 0, updated);
    } else {
      nextDiscover.push(updated);
    }
    return {
      additionalDiscoverBooks: nextDiscover,
      additionalDecisionBooks: reconciled.additionalDecisionBooks,
    };
  }

  if (nextStatus === "rejected" || nextStatus === "maybe_later") {
    const nextDecisions = [...reconciled.additionalDecisionBooks];
    if (
      restoreDecisionIndex !== undefined &&
      restoreDecisionIndex >= 0 &&
      restoreDecisionIndex <= nextDecisions.length
    ) {
      nextDecisions.splice(restoreDecisionIndex, 0, updated);
    } else {
      nextDecisions.push(updated);
    }
    return {
      additionalDiscoverBooks: reconciled.additionalDiscoverBooks,
      additionalDecisionBooks: nextDecisions,
    };
  }

  return reconciled;
}

export function applyReadingHistoryScores<
  T extends {
    id: number;
    algorithm_score: number | null;
    algorithm_explanation: string[];
    metadata_confidence: number | null;
  },
>(
  items: readonly T[],
  scoresById: ReadonlyMap<number, ReadingHistoryScorePatch>,
): T[] {
  if (scoresById.size === 0) return [...items];
  return items.map((item) => {
    const score = scoresById.get(item.id);
    return score
      ? {
          ...item,
          algorithm_score: score.algorithm_score,
          algorithm_explanation: score.algorithm_explanation,
          metadata_confidence: score.metadata_confidence,
        }
      : item;
  });
}

export function sortReadingHistory<
  T extends {
    id: number;
    title: string;
    rating: number | null;
    read_at: string | null;
    created_at: string;
    algorithm_score: number | null;
  },
>(
  items: readonly T[],
  sort: ReadingHistorySort,
  scoresPending = false,
): T[] {
  const hasReadDate = (item: T) => Boolean(item.read_at && item.read_at.trim());
  const timestamps = new Map<number, number>();
  const readTimestamp = (item: T) => {
    const cached = timestamps.get(item.id);
    if (cached !== undefined) return cached;
    const raw = hasReadDate(item) ? item.read_at! : item.created_at;
    const timestamp = Date.parse(raw);
    const normalized = Number.isFinite(timestamp) ? timestamp : 0;
    timestamps.set(item.id, normalized);
    return normalized;
  };
  const compareDatedChronology = (left: T, right: T, ascending: boolean) => {
    const leftDated = hasReadDate(left);
    const rightDated = hasReadDate(right);
    if (leftDated !== rightDated) return leftDated ? -1 : 1;
    const delta = ascending
      ? readTimestamp(left) - readTimestamp(right)
      : readTimestamp(right) - readTimestamp(left);
    return delta || left.title.localeCompare(right.title);
  };

  return [...items].sort((left, right) => {
    if (sort === "oldest") {
      return compareDatedChronology(left, right, true);
    }
    if (sort === "score_high" || sort === "score_low") {
      if (scoresPending && left.algorithm_score === null && right.algorithm_score === null) {
        return compareDatedChronology(left, right, false);
      }
      if (left.algorithm_score === null) {
        return right.algorithm_score === null ? left.title.localeCompare(right.title) : 1;
      }
      if (right.algorithm_score === null) return -1;
      const scoreOrder =
        sort === "score_high"
          ? right.algorithm_score - left.algorithm_score
          : left.algorithm_score - right.algorithm_score;
      return scoreOrder || left.title.localeCompare(right.title);
    }
    if (sort === "rating_high") {
      return (right.rating ?? 0) - (left.rating ?? 0) || left.title.localeCompare(right.title);
    }
    if (sort === "title") {
      return left.title.localeCompare(right.title);
    }
    return compareDatedChronology(left, right, false);
  });
}
