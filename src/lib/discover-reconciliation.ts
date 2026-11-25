import type { SubmitFunction } from "$app/forms";
import { actionResultMessage } from "./action-result-message.js";

export type ActionResultLike = {
  type: string;
  status?: number;
  data?: unknown;
  error?: unknown;
};

export type ReconcilableBook = {
  id: number;
  status: string;
  recommendation_run_id?: string;
};

export type OptimisticChange = {
  commit?: (result?: ActionResultLike) => void;
  rollback?: () => void;
};

export type OptimisticFactory = (formData: FormData) => OptimisticChange | void;

export type PendingOptions = {
  localOnly?: boolean;
  refreshDiscoverRun?: boolean;
  refreshDiscoverIfOffView?: boolean;
};

export type DiscoverSessionState<T extends ReconcilableBook> = {
  activeRunIdOverride: string | null;
  discoverSessionInitializedOverride: boolean;
  clientDiscoverBooks: T[] | null;
  additionalDiscoverBooks: T[];
  sessionBooksById: Record<number, T>;
  bookStatusOverrides: Record<number, string>;
  committedBookStatuses: Record<number, string>;
  localReadBookIds: Set<number>;
  localRecentReads: Array<{
    id: number;
    title: string;
    author: string;
    rating: number | null;
  }>;
  discoverLoadedCountOverride: number | null;
  discoverHasMoreOverride: boolean | null;
  discoverLoadError: string;
};

export function extractActionBookStatus(
  result?: ActionResultLike,
): string | undefined {
  if (!result || result.type !== "success" || !result.data || typeof result.data !== "object") {
    return undefined;
  }
  const status = (result.data as Record<string, unknown>).status;
  return typeof status === "string" && status.trim() ? status.trim() : undefined;
}

export function extractActionBookId(
  result: ActionResultLike | undefined,
  fallbackId: number,
): number {
  if (result && result.type === "success" && result.data && typeof result.data === "object") {
    const id = (result.data as Record<string, unknown>).id;
    if (typeof id === "number" && Number.isInteger(id) && id > 0) {
      return id;
    }
  }
  return fallbackId;
}

export function extractActionReadId(
  result: ActionResultLike | undefined,
): number | undefined {
  if (result && result.type === "success" && result.data && typeof result.data === "object") {
    const readId = (result.data as Record<string, unknown>).read_id;
    if (typeof readId === "number" && Number.isInteger(readId) && readId > 0) {
      return readId;
    }
  }
  return undefined;
}

export function extractActionRestoredItems(
  result: ActionResultLike | undefined,
  fallbackIds: number[] = [],
): Array<{ id: number; status: string }> {
  if (!result || result.type !== "success" || !result.data || typeof result.data !== "object") {
    return [];
  }
  const data = result.data as Record<string, unknown>;
  if (Array.isArray(data.items)) {
    return data.items
      .filter(
        (item): item is { id: number; status: string } =>
          Boolean(item) &&
          typeof item === "object" &&
          typeof (item as Record<string, unknown>).id === "number" &&
          Number.isInteger((item as Record<string, unknown>).id) &&
          ((item as Record<string, unknown>).id as number) > 0 &&
          typeof (item as Record<string, unknown>).status === "string" &&
          Boolean(((item as Record<string, unknown>).status as string).trim()),
      )
      .map((item) => ({ id: item.id, status: item.status.trim() }));
  }
  const singleStatus = extractActionBookStatus(result);
  if (!singleStatus) return [];
  const singleId = extractActionBookId(result, fallbackIds[0] ?? 0);
  return singleId > 0 ? [{ id: singleId, status: singleStatus }] : [];
}

export function createOptimisticBookStatus(
  deps: {
    hasBook: (id: number) => boolean;
    getOverrides: () => Record<number, string>;
    setOverrides: (next: Record<number, string>) => void;
  },
  id: number,
  status: string,
  keepCommitted = false,
): OptimisticChange | void {
  if (!Number.isInteger(id) || id <= 0 || !deps.hasBook(id)) return undefined;
  const currentOverrides = deps.getOverrides();
  const previous = currentOverrides[id];
  deps.setOverrides({ ...currentOverrides, [id]: status });
  return {
    commit: () => {
      if (keepCommitted) return;
      const latest = deps.getOverrides();
      if (latest[id] !== status) return;
      const next = { ...latest };
      delete next[id];
      deps.setOverrides(next);
    },
    rollback: () => {
      const latest = deps.getOverrides();
      if (latest[id] !== status) return;
      const next = { ...latest };
      if (previous === undefined) delete next[id];
      else next[id] = previous;
      deps.setOverrides(next);
    },
  };
}

export function buildBaseBooks<T extends ReconcilableBook>(
  serverBooks: T[],
  clientDiscoverBooks: T[] | null,
): T[] {
  if (!clientDiscoverBooks) return serverBooks;
  const discoverIds = new Set(clientDiscoverBooks.map((book) => book.id));
  return [
    ...clientDiscoverBooks,
    ...serverBooks.filter(
      (book) => book.status !== "recommended" && !discoverIds.has(book.id),
    ),
  ];
}

export function mergeReconciledBooks<T extends ReconcilableBook>(params: {
  baseBooks: T[];
  additionalDiscoverBooks: T[];
  decisions: T[];
  additionalDecisionBooks: T[];
  sessionBooksById?: Record<number, T>;
  committedBookStatuses: Record<number, string>;
  bookStatusOverrides: Record<number, string>;
}): T[] {
  const {
    baseBooks,
    additionalDiscoverBooks,
    decisions,
    additionalDecisionBooks,
    sessionBooksById = {},
    committedBookStatuses,
    bookStatusOverrides,
  } = params;
  const seen = new Set<number>();
  const merged: T[] = [];
  const sources: T[] = [
    ...baseBooks,
    ...additionalDiscoverBooks,
    ...decisions,
    ...additionalDecisionBooks,
  ];
  for (const idStr of Object.keys(committedBookStatuses)) {
    const id = Number(idStr);
    const cached = sessionBooksById[id];
    if (cached) sources.push(cached);
  }
  for (const book of sources) {
    if (seen.has(book.id)) continue;
    seen.add(book.id);
    const effectiveStatus =
      bookStatusOverrides[book.id] ?? committedBookStatuses[book.id];
    merged.push(
      effectiveStatus === undefined ? book : { ...book, status: effectiveStatus },
    );
  }
  return merged;
}

export function commitLocalBookStatus<T extends ReconcilableBook>(
  state: Pick<
    DiscoverSessionState<T>,
    "committedBookStatuses" | "sessionBooksById"
  >,
  book: T | undefined,
  id: number,
  status: string,
): Pick<DiscoverSessionState<T>, "committedBookStatuses" | "sessionBooksById"> {
  if (!Number.isInteger(id) || id <= 0 || !status.trim()) {
    return {
      committedBookStatuses: state.committedBookStatuses,
      sessionBooksById: state.sessionBooksById,
    };
  }
  return {
    committedBookStatuses: {
      ...state.committedBookStatuses,
      [id]: status.trim(),
    },
    sessionBooksById: book
      ? { ...state.sessionBooksById, [id]: book }
      : state.sessionBooksById,
  };
}

export function reconcileOverviewBooksReload<T extends ReconcilableBook>(params: {
  state: DiscoverSessionState<T>;
  previousBooksSnapshot: T[] | null;
  priorRunId: string;
  nextBooks: T[];
  nextDecisions: T[];
  nextRunId: string;
}): DiscoverSessionState<T> | null {
  const {
    state,
    previousBooksSnapshot,
    priorRunId,
    nextBooks,
    nextDecisions,
    nextRunId,
  } = params;
  if (previousBooksSnapshot === null) {
    return null;
  }

  if (!nextRunId) {
    // Non-Discover reloads use track_run=false. Always clear client overrides
    // and committed statuses so fresh server truth in nextBooks/nextDecisions
    // is never shadowed, while keeping any active Discover session's remaining
    // recommended slate and pagination slices intact.
    const serverNonRecommendedIds = new Set<number>();
    for (const book of nextBooks) {
      if (book.status !== "recommended") serverNonRecommendedIds.add(book.id);
    }
    for (const book of nextDecisions) {
      serverNonRecommendedIds.add(book.id);
    }
    for (const readId of state.localReadBookIds) {
      serverNonRecommendedIds.add(readId);
    }

    const effectiveBeforeReload = (book: T) =>
      state.bookStatusOverrides[book.id] ??
      state.committedBookStatuses[book.id] ??
      book.status;

    const filterPreservedRecommended = (books: T[]) =>
      books.filter(
        (book) =>
          effectiveBeforeReload(book) === "recommended" &&
          !serverNonRecommendedIds.has(book.id),
      );

    const preservedRunId = state.activeRunIdOverride ?? priorRunId;
    let nextClientDiscover = state.clientDiscoverBooks
      ? filterPreservedRecommended(state.clientDiscoverBooks)
      : null;
    let nextLoadedCount = state.discoverLoadedCountOverride;
    let nextInitialized = state.discoverSessionInitializedOverride;
    let nextActiveRunOverlay = state.activeRunIdOverride;

    if (preservedRunId) {
      nextActiveRunOverlay = preservedRunId;
      nextInitialized = true;
      if (!state.clientDiscoverBooks) {
        const priorRecommended = previousBooksSnapshot.filter(
          (book) => book.status === "recommended",
        );
        nextLoadedCount = nextLoadedCount ?? priorRecommended.length;
        nextClientDiscover = filterPreservedRecommended(priorRecommended);
      }
    }

    const nextAdditionalDiscover = filterPreservedRecommended(
      state.additionalDiscoverBooks,
    );
    const nextSessionBooks: Record<number, T> = { ...state.sessionBooksById };
    for (const book of [
      ...(nextClientDiscover ?? []),
      ...nextAdditionalDiscover,
      ...nextBooks,
      ...nextDecisions,
    ]) {
      nextSessionBooks[book.id] = book;
    }

    return {
      ...state,
      activeRunIdOverride: nextActiveRunOverlay,
      discoverSessionInitializedOverride: nextInitialized,
      clientDiscoverBooks: nextClientDiscover,
      additionalDiscoverBooks: nextAdditionalDiscover,
      sessionBooksById: nextSessionBooks,
      bookStatusOverrides: {},
      committedBookStatuses: {},
      localReadBookIds: new Set(),
      localRecentReads: [],
      discoverLoadedCountOverride: nextLoadedCount,
    };
  }

  return {
    activeRunIdOverride: null,
    discoverSessionInitializedOverride: true,
    clientDiscoverBooks: null,
    additionalDiscoverBooks: [],
    sessionBooksById: {},
    bookStatusOverrides: {},
    committedBookStatuses: {},
    localReadBookIds: new Set(),
    localRecentReads: [],
    discoverLoadedCountOverride: null,
    discoverHasMoreOverride: null,
    discoverLoadError: "",
  };
}

export function mergeDiscoverPaginationSlice<T extends ReconcilableBook>(params: {
  existingBooks: T[];
  additionalDiscoverBooks: T[];
  incomingItems: T[];
  hasMore: boolean;
  responseRunId?: string;
  activeRunId: string;
  discoverLoadedCount: number;
  discoverVisibleCount: number;
  priorFilteredCount: number;
  pageSize: number;
}): {
  additionalDiscoverBooks: T[];
  discoverLoadedCountOverride: number;
  discoverHasMoreOverride: boolean;
  discoverVisibleCount: number;
  activeRunIdOverride?: string;
  discoverSessionInitializedOverride?: boolean;
} {
  const {
    existingBooks,
    additionalDiscoverBooks,
    incomingItems,
    hasMore,
    responseRunId,
    activeRunId,
    discoverLoadedCount,
    discoverVisibleCount,
    priorFilteredCount,
    pageSize,
  } = params;
  const existingIds = new Set(existingBooks.map((book) => book.id));
  const newItems = incomingItems.filter((book) => !existingIds.has(book.id));
  const nextRunPatch =
    responseRunId && !activeRunId
      ? {
          activeRunIdOverride: responseRunId,
          discoverSessionInitializedOverride: true,
        }
      : {};
  return {
    ...nextRunPatch,
    additionalDiscoverBooks: [...additionalDiscoverBooks, ...newItems],
    discoverLoadedCountOverride: discoverLoadedCount + incomingItems.length,
    discoverHasMoreOverride: hasMore,
    discoverVisibleCount: Math.max(
      discoverVisibleCount + pageSize,
      priorFilteredCount + newItems.length,
    ),
  };
}

export function resolveLibrarrDownloadReconciliation(
  payload: { candidate?: { id?: unknown; status?: unknown } } | null | undefined,
  activeView: string,
): {
  candidateStatus?: string;
  shouldInvalidate: boolean;
} {
  const rawStatus = payload?.candidate?.status;
  const candidateStatus =
    typeof rawStatus === "string" && rawStatus.trim()
      ? rawStatus.trim()
      : undefined;
  return {
    candidateStatus,
    shouldInvalidate: activeView !== "discover" || !candidateStatus,
  };
}

export function createOptimisticImport(
  formData: FormData,
  onCommitStatus: (id: number, status: string) => void,
): OptimisticChange | void {
  const id = Number(formData.get("id"));
  if (!Number.isInteger(id) || id <= 0) return undefined;
  return {
    commit: (result) => {
      // Never hard-code "imported" client-side: derive any post-action status
      // from the server response (or let the non-localOnly Shortlist reload
      // fetch the unified server status, e.g. "imported" vs "read").
      const serverStatus = extractActionBookStatus(result);
      if (serverStatus) {
        onCommitStatus(extractActionBookId(result, id), serverStatus);
      }
    },
  };
}

export function createPendingSubmit(deps: {
  key: string;
  optimistic?: OptimisticFactory;
  options?: PendingOptions;
  setPendingState: (key: string, pending: boolean) => void;
  flushTelemetry: () => Promise<void>;
  clearOptimisticNotification: () => void;
  announceActionError: (message: string) => void;
  onResetReadingHistory?: () => void;
  onResetClientPagination?: () => void;
  onReloadFilteredLists?: () => void;
  onRefreshDiscoverRun?: () => Promise<void>;
  onRefreshDiscoverIfOffView?: () => Promise<void>;
}): SubmitFunction {
  const {
    key,
    optimistic,
    options,
    setPendingState,
    flushTelemetry,
    clearOptimisticNotification,
    announceActionError,
    onResetReadingHistory,
    onResetClientPagination,
    onReloadFilteredLists,
    onRefreshDiscoverRun,
    onRefreshDiscoverIfOffView,
  } = deps;

  return async ({ formData }) => {
    setPendingState(key, true);
    let change: OptimisticChange | void;
    try {
      change = optimistic?.(formData);
    } catch {
      change = undefined;
    }
    if (options?.localOnly) {
      await flushTelemetry();
    }
    return async ({ update, result }) => {
      const succeeded =
        result.type === "success" || result.type === "redirect";
      const rollback = () => {
        try {
          change?.rollback?.();
        } catch {
          // A rollback must never obscure the action result.
        }
      };
      try {
        if (!succeeded) {
          rollback();
        }
        if (result.type === "error") {
          announceActionError(actionResultMessage(result));
          return;
        }
        clearOptimisticNotification();
        if (options?.localOnly) {
          await update({ reset: false, refreshAll: false, navigate: false });
        } else {
          await update({ reset: succeeded, navigate: false });
        }
        if (
          succeeded &&
          (key === "sync" ||
            key === "restore-backup" ||
            key === "goodreads-rss" ||
            key === "csv-import" ||
            key === "goodreads-csv" ||
            key === "toggle-default" ||
            key.startsWith("toggle-source-"))
        ) {
          onResetClientPagination?.();
        }
        if (
          succeeded &&
          (key.startsWith("read-") ||
            key.startsWith("shortlist-read-") ||
            key === "goodreads-rss" ||
            key === "csv-import" ||
            key === "goodreads-csv" ||
            key === "restore-backup")
        ) {
          onResetReadingHistory?.();
        }
        if (succeeded) {
          change?.commit?.(result);
          if (
            key === "sync" ||
            key === "rebuild-embeddings" ||
            key === "restore-backup" ||
            key === "goodreads-rss" ||
            key === "csv-import" ||
            key === "goodreads-csv" ||
            key === "source" ||
            key.startsWith("source-") ||
            key.startsWith("refresh-source-") ||
            key === "toggle-default" ||
            key.startsWith("toggle-source-")
          ) {
            onReloadFilteredLists?.();
          }
          if (options?.refreshDiscoverRun || options?.refreshDiscoverIfOffView) {
            await (onRefreshDiscoverRun ?? onRefreshDiscoverIfOffView)?.();
          }
        }
      } catch (error) {
        rollback();
        announceActionError(
          error instanceof Error
            ? error.message
            : "The request could not be completed. Your change was reverted.",
        );
      } finally {
        setPendingState(key, false);
      }
    };
  };
}
