export const LIVE_STATUS_DEBOUNCE_MS = 300;

export type LiveStatusUpdateOptions = {
  immediate?: boolean;
  contextKey?: string;
  paused?: boolean;
};

export type LiveStatusControllerOptions = {
  debounceMs?: number;
  announceOnMount?: boolean;
  onAnnounce: (message: string) => void;
};

export type LiveStatusController = {
  update: (
    message: string | null | undefined,
    options?: LiveStatusUpdateOptions,
  ) => void;
  destroy: () => void;
};

export function createLiveStatusController({
  debounceMs = LIVE_STATUS_DEBOUNCE_MS,
  announceOnMount = false,
  onAnnounce,
}: LiveStatusControllerOptions): LiveStatusController {
  let initialized = false;
  let lastInput = "";
  let lastAnnounced = "";
  let lastContextKey: string | undefined = undefined;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function clearTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function deliver(message: string) {
    if (message !== lastAnnounced) {
      lastAnnounced = message;
      onAnnounce(message);
    }
  }

  return {
    update(rawMessage, options) {
      const message = (rawMessage ?? "").trim();
      const immediate = Boolean(options?.immediate);
      const contextKey = options?.contextKey;
      const paused = Boolean(options?.paused);

      if (!initialized) {
        initialized = true;
        lastInput = message;
        lastContextKey = contextKey;
        if (!paused && announceOnMount && message) {
          timer = setTimeout(() => {
            timer = null;
            deliver(message);
          }, 0);
        }
        return;
      }

      if (paused) {
        clearTimer();
        lastInput = message;
        lastContextKey = contextKey;
        return;
      }

      if (contextKey !== lastContextKey) {
        lastContextKey = contextKey;
        lastInput = message;
        clearTimer();
        if (announceOnMount && message) {
          timer = setTimeout(() => {
            timer = null;
            deliver(message);
          }, 0);
        } else {
          deliver("");
        }
        return;
      }

      if (message === lastInput) {
        if (immediate && timer !== null) {
          clearTimer();
          deliver(message);
        }
        return;
      }

      lastInput = message;
      clearTimer();

      if (!message) {
        deliver("");
        return;
      }

      if (immediate) {
        deliver(message);
        return;
      }

      timer = setTimeout(() => {
        timer = null;
        deliver(message);
      }, debounceMs);
    },
    destroy() {
      clearTimer();
    },
  };
}

export type LiveStatusActionParams = {
  message?: string | null;
  immediate?: boolean;
  announceOnMount?: boolean;
  debounceMs?: number;
  contextKey?: string;
  paused?: boolean;
};

export function liveStatus(
  node: { textContent: string | null },
  initialParams: LiveStatusActionParams = {},
) {
  let currentConfig = {
    debounceMs: initialParams.debounceMs ?? LIVE_STATUS_DEBOUNCE_MS,
    announceOnMount: Boolean(initialParams.announceOnMount),
  };
  let controller = createLiveStatusController({
    ...currentConfig,
    onAnnounce: (nextMessage) => {
      node.textContent = nextMessage;
    },
  });
  controller.update(initialParams.message, {
    immediate: initialParams.immediate,
    contextKey: initialParams.contextKey,
    paused: initialParams.paused,
  });

  return {
    update(nextParams: LiveStatusActionParams = {}) {
      const nextDebounceMs = nextParams.debounceMs ?? LIVE_STATUS_DEBOUNCE_MS;
      const nextAnnounceOnMount = Boolean(nextParams.announceOnMount);
      if (
        nextDebounceMs !== currentConfig.debounceMs ||
        nextAnnounceOnMount !== currentConfig.announceOnMount
      ) {
        controller.destroy();
        currentConfig = {
          debounceMs: nextDebounceMs,
          announceOnMount: nextAnnounceOnMount,
        };
        controller = createLiveStatusController({
          ...currentConfig,
          onAnnounce: (nextMessage) => {
            node.textContent = nextMessage;
          },
        });
      }
      controller.update(nextParams.message, {
        immediate: nextParams.immediate,
        contextKey: nextParams.contextKey,
        paused: nextParams.paused,
      });
    },
    destroy() {
      controller.destroy();
    },
  };
}

export type RecommendationStatusInput = {
  view: "discover" | "saved" | "decisions";
  visibleCount: number;
  matchedCount: number;
  shortlistTotalCount: number;
  totalDecisionCount: number;
  hasActiveFilter: boolean;
  decisionFilter: "all" | "maybe_later" | "rejected" | "read";
  digestVisible: boolean;
  digestRequested?: boolean;
  discoverHasMore: boolean;
  loading?: boolean;
  hasError?: boolean;
};

export function formatRecommendationStatus({
  view,
  visibleCount,
  matchedCount,
  shortlistTotalCount,
  totalDecisionCount,
  hasActiveFilter,
  decisionFilter,
  digestVisible,
  digestRequested = false,
  discoverHasMore,
  loading = false,
  hasError = false,
}: RecommendationStatusInput): string {
  if (hasError) return "";
  if (loading) {
    return view === "decisions"
      ? "Loading past decisions…"
      : "Searching recommendations…";
  }

  if (matchedCount === 0) {
    if (view === "saved" && shortlistTotalCount === 0) {
      return "Your shortlist is empty.";
    }
    if (hasActiveFilter) {
      return view === "saved"
        ? "No shortlist books match these filters."
        : "No books match these filters.";
    }
    if (view === "decisions") {
      if (decisionFilter === "maybe_later") return "No deferred books.";
      if (decisionFilter === "rejected") return "No passed books.";
      return "No past decisions.";
    }
    if (digestRequested) {
      return "No unread digest picks left.";
    }
    if (digestVisible) {
      return "No digest picks yet.";
    }
    if (view === "saved") {
      return "No shortlist books match these filters.";
    }
    return "You are all caught up.";
  }

  if (view === "saved") {
    if (hasActiveFilter) {
      return `Showing ${matchedCount} of ${shortlistTotalCount} shortlist ${shortlistTotalCount === 1 ? "book" : "books"}.`;
    }
    return `Showing ${matchedCount} shortlist ${matchedCount === 1 ? "book" : "books"}.`;
  }

  if (view === "decisions") {
    if ((hasActiveFilter || decisionFilter !== "all") && totalDecisionCount >= matchedCount) {
      return `Showing ${matchedCount} of ${totalDecisionCount} past ${totalDecisionCount === 1 ? "decision" : "decisions"}.`;
    }
    return `Showing ${matchedCount} past ${matchedCount === 1 ? "decision" : "decisions"}.`;
  }

  if (hasActiveFilter) {
    if (visibleCount < matchedCount) {
      return `Showing ${visibleCount} of ${matchedCount} matching recommendations.`;
    }
    if (discoverHasMore) {
      return `Showing ${visibleCount} matching ${visibleCount === 1 ? "recommendation" : "recommendations"}.`;
    }
    return `${matchedCount} ${matchedCount === 1 ? "recommendation matches" : "recommendations match"} these filters.`;
  }

  if (visibleCount < matchedCount || discoverHasMore) {
    return `Showing ${visibleCount} ${visibleCount === 1 ? "recommendation" : "recommendations"}.`;
  }

  return `Showing all ${matchedCount} ${matchedCount === 1 ? "recommendation" : "recommendations"}.`;
}

export type ReadingHistoryStatusInput = {
  state: "idle" | "loading" | "ready" | "error";
  visibleCount: number;
  matchedCount: number;
  hasActiveFilter: boolean;
};

export function formatReadingHistoryStatus({
  state,
  visibleCount,
  matchedCount,
  hasActiveFilter,
}: ReadingHistoryStatusInput): string {
  if (state === "loading") return "Loading your read books…";
  if (state !== "ready") return "";
  if (matchedCount === 0) {
    return hasActiveFilter
      ? "No read books match these filters. Change the search, rating, or source filter to see more books."
      : "No read books yet. Import a Goodreads or StoryGraph CSV, or a Goodreads read-shelf feed, in Settings to see your reading history here.";
  }
  return `Showing ${visibleCount} of ${matchedCount} ${matchedCount === 1 ? "book" : "books"}.`;
}

export type LlmCatalogStatusInput = {
  loaded: boolean;
  loading: boolean;
  hasError: boolean;
  totalProviders: number;
  matchedProviders: number;
  providerQuery: string;
  selectedProviderName: string | null;
  totalModels: number;
  matchedModels: number;
  modelQuery: string;
  activeFilter: "none" | "provider" | "model";
};

export function formatLlmCatalogStatus({
  loaded,
  loading,
  hasError,
  totalProviders,
  matchedProviders,
  providerQuery,
  selectedProviderName,
  totalModels,
  matchedModels,
  modelQuery,
  activeFilter,
}: LlmCatalogStatusInput): string {
  if (loading || hasError || !loaded) return "";
  if (totalProviders === 0) {
    return "No providers found in models.dev catalog.";
  }

  const trimmedProviderQuery = providerQuery.trim();
  const trimmedModelQuery = modelQuery.trim();

  if (activeFilter === "model" && selectedProviderName) {
    if (!trimmedModelQuery) {
      return `Showing all ${totalModels} ${totalModels === 1 ? "model" : "models"} for ${selectedProviderName}.`;
    }
    if (matchedModels === 0) {
      return `No models match "${trimmedModelQuery}" for ${selectedProviderName}.`;
    }
    return `${matchedModels} of ${totalModels} ${totalModels === 1 ? "model matches" : "models match"} for ${selectedProviderName}.`;
  }

  if (activeFilter === "provider") {
    if (!trimmedProviderQuery) {
      return `Showing all ${totalProviders} ${totalProviders === 1 ? "provider" : "providers"}.`;
    }
    if (matchedProviders === 0) {
      return `No providers match "${trimmedProviderQuery}".`;
    }
    return `${matchedProviders} of ${totalProviders} ${totalProviders === 1 ? "provider matches" : "providers match"}.`;
  }

  if (selectedProviderName) {
    return `Loaded ${totalProviders} ${totalProviders === 1 ? "provider" : "providers"} from models.dev. ${selectedProviderName} has ${totalModels} ${totalModels === 1 ? "model" : "models"}.`;
  }

  return `Loaded ${totalProviders} ${totalProviders === 1 ? "provider" : "providers"} from models.dev.`;
}

export type LibrarrSearchStatusInput = {
  state: "idle" | "searching" | "ready" | "error";
  resultCount: number;
  mediaType: "ebook" | "audiobook";
};

export function formatLibrarrSearchStatus({
  state,
  resultCount,
  mediaType,
}: LibrarrSearchStatusInput): string {
  if (state === "searching") {
    if (resultCount > 0) {
      return `Searching Librarr… ${resultCount} ${resultCount === 1 ? "match" : "matches"} so far.`;
    }
    return "Searching Librarr…";
  }
  if (state === "ready") {
    if (resultCount === 0) {
      return `No ${mediaType} results found. Try a title, author, or a shorter search.`;
    }
    return `Found ${resultCount} ${mediaType} ${resultCount === 1 ? "result" : "results"}.`;
  }
  return "";
}
