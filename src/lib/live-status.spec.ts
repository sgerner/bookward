import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  LIVE_STATUS_DEBOUNCE_MS,
  createLiveStatusController,
  formatLibrarrSearchStatus,
  formatLlmCatalogStatus,
  formatReadingHistoryStatus,
  formatRecommendationStatus,
} from "./live-status";

describe("live status controller", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("suppresses initial mount announcements by default so tab switches stay silent", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("Showing 8 recommendations.");
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);

    expect(onAnnounce).not.toHaveBeenCalled();
    controller.destroy();
  });

  it("debounces rapid filter updates by 300 ms and announces only the settled count", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("Showing 8 recommendations.");
    controller.update("5 recommendations match these filters.");
    vi.advanceTimersByTime(150);
    controller.update("2 recommendations match these filters.");
    vi.advanceTimersByTime(150);
    controller.update("No books match these filters.");

    expect(onAnnounce).not.toHaveBeenCalled();

    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS - 1);
    expect(onAnnounce).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    expect(onAnnounce).toHaveBeenCalledWith("No books match these filters.");
    controller.destroy();
  });

  it("delivers load completions immediately and cancels pending debounced updates", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("");
    controller.update("Searching Librarr… 1 match so far.");
    vi.advanceTimersByTime(100);

    controller.update("Found 4 audiobook results.", { immediate: true });
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    expect(onAnnounce).toHaveBeenCalledWith("Found 4 audiobook results.");

    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    controller.destroy();
  });

  it("resets baseline silently when contextKey changes across views", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("Showing 8 of more recommendations.", {
      contextKey: "discover",
    });
    controller.update("Showing 16 of more recommendations.", {
      contextKey: "discover",
      immediate: true,
    });
    expect(onAnnounce).toHaveBeenCalledTimes(1);

    onAnnounce.mockClear();
    controller.update("3 books on your shortlist.", {
      contextKey: "saved",
      immediate: true,
    });
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    // Clears previous view's live-region text to "" without announcing the new view's baseline count
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    expect(onAnnounce).toHaveBeenCalledWith("");

    onAnnounce.mockClear();
    controller.update("1 of 3 shortlist books match.", {
      contextKey: "saved",
    });
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS);
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    expect(onAnnounce).toHaveBeenCalledWith("1 of 3 shortlist books match.");
    controller.destroy();
  });

  it("suppresses reactive updates and cancels pending timers while paused during view transitions", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("Showing 8 recommendations.", { contextKey: "discover" });
    controller.update("4 recommendations match these filters.", {
      contextKey: "discover",
    });
    vi.advanceTimersByTime(100);

    controller.update("No books match these filters.", {
      contextKey: "discover",
      paused: true,
      immediate: true,
    });
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(onAnnounce).not.toHaveBeenCalled();
    controller.destroy();
  });

  it("announces initial mount asynchronously when announceOnMount is enabled for modal dialogs", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({
      onAnnounce,
      announceOnMount: true,
    });

    controller.update("Searching Librarr…", { immediate: true });
    expect(onAnnounce).not.toHaveBeenCalled();

    vi.advanceTimersByTime(0);
    expect(onAnnounce).toHaveBeenCalledTimes(1);
    expect(onAnnounce).toHaveBeenCalledWith("Searching Librarr…");

    controller.update("No ebook results found. Try a title, author, or a shorter search.", {
      immediate: true,
    });
    expect(onAnnounce).toHaveBeenCalledTimes(2);
    expect(onAnnounce).toHaveBeenLastCalledWith(
      "No ebook results found. Try a title, author, or a shorter search.",
    );
    controller.destroy();
  });

  it("clears empty messages immediately and cancels pending timers on destroy", () => {
    const onAnnounce = vi.fn();
    const controller = createLiveStatusController({ onAnnounce });

    controller.update("");
    controller.update("Copied to clipboard.", { immediate: true });
    expect(onAnnounce).toHaveBeenCalledWith("Copied to clipboard.");

    controller.update("");
    expect(onAnnounce).toHaveBeenLastCalledWith("");

    controller.update("Pending update");
    controller.destroy();
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(onAnnounce).toHaveBeenCalledTimes(2);
  });
});

describe("status message formatters", () => {
  it("formats Discover, Shortlist, and Past Decisions counts, loading, error, and empty states", () => {
    expect(
      formatRecommendationStatus({
        view: "discover",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
        loading: true,
      }),
    ).toBe("Searching recommendations…");

    expect(
      formatRecommendationStatus({
        view: "decisions",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
        loading: true,
      }),
    ).toBe("Loading past decisions…");

    expect(
      formatRecommendationStatus({
        view: "discover",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
        hasError: true,
      }),
    ).toBe("");

    expect(
      formatRecommendationStatus({
        view: "discover",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: false,
        decisionFilter: "all",
        digestVisible: false,
        digestRequested: true,
        discoverHasMore: false,
      }),
    ).toBe("No unread digest picks left.");

    expect(
      formatRecommendationStatus({
        view: "discover",
        visibleCount: 3,
        matchedCount: 3,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("3 recommendations match these filters.");

    expect(
      formatRecommendationStatus({
        view: "discover",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("No books match these filters.");

    expect(
      formatRecommendationStatus({
        view: "saved",
        visibleCount: 1,
        matchedCount: 1,
        shortlistTotalCount: 4,
        totalDecisionCount: 0,
        hasActiveFilter: true,
        decisionFilter: "all",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("Showing 1 of 4 shortlist books.");

    expect(
      formatRecommendationStatus({
        view: "decisions",
        visibleCount: 2,
        matchedCount: 2,
        shortlistTotalCount: 0,
        totalDecisionCount: 5,
        hasActiveFilter: false,
        decisionFilter: "rejected",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("Showing 2 of 5 past decisions.");

    expect(
      formatRecommendationStatus({
        view: "decisions",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 4,
        hasActiveFilter: false,
        decisionFilter: "maybe_later",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("No deferred books.");

    expect(
      formatRecommendationStatus({
        view: "decisions",
        visibleCount: 0,
        matchedCount: 0,
        shortlistTotalCount: 0,
        totalDecisionCount: 4,
        hasActiveFilter: false,
        decisionFilter: "rejected",
        digestVisible: false,
        discoverHasMore: false,
      }),
    ).toBe("No passed books.");
  });

  it("formats Reading History loading, counts, and filtered zero-match states", () => {
    expect(
      formatReadingHistoryStatus({
        state: "loading",
        visibleCount: 0,
        matchedCount: 0,
        hasActiveFilter: false,
      }),
    ).toBe("Loading your read books…");

    expect(
      formatReadingHistoryStatus({
        state: "ready",
        visibleCount: 12,
        matchedCount: 12,
        hasActiveFilter: false,
      }),
    ).toBe("Showing 12 of 12 books.");

    expect(
      formatReadingHistoryStatus({
        state: "ready",
        visibleCount: 0,
        matchedCount: 0,
        hasActiveFilter: true,
      }),
    ).toBe(
      "No read books match these filters. Change the search, rating, or source filter to see more books.",
    );

    expect(
      formatReadingHistoryStatus({
        state: "ready",
        visibleCount: 0,
        matchedCount: 0,
        hasActiveFilter: false,
      }),
    ).toBe(
      "No read books yet. Import a Goodreads or StoryGraph CSV, or a Goodreads read-shelf feed, in Settings to see your reading history here.",
    );
  });

  it("formats models.dev catalog loads and provider/model filter counts", () => {
    expect(
      formatLlmCatalogStatus({
        loaded: true,
        loading: false,
        hasError: false,
        totalProviders: 24,
        matchedProviders: 24,
        providerQuery: "",
        selectedProviderName: null,
        totalModels: 0,
        matchedModels: 0,
        modelQuery: "",
        activeFilter: "none",
      }),
    ).toBe("Loaded 24 providers from models.dev.");

    expect(
      formatLlmCatalogStatus({
        loaded: true,
        loading: false,
        hasError: false,
        totalProviders: 24,
        matchedProviders: 0,
        providerQuery: "none",
        selectedProviderName: null,
        totalModels: 0,
        matchedModels: 0,
        modelQuery: "",
        activeFilter: "provider",
      }),
    ).toBe('No providers match "none".');

    expect(
      formatLlmCatalogStatus({
        loaded: true,
        loading: false,
        hasError: false,
        totalProviders: 24,
        matchedProviders: 24,
        providerQuery: "",
        selectedProviderName: "OpenAI",
        totalModels: 8,
        matchedModels: 2,
        modelQuery: "gpt",
        activeFilter: "model",
      }),
    ).toBe("2 of 8 models match for OpenAI.");
  });

  it("formats Librarr search initial, populated, and zero-result states", () => {
    expect(
      formatLibrarrSearchStatus({
        state: "searching",
        resultCount: 0,
        mediaType: "audiobook",
      }),
    ).toBe("Searching Librarr…");

    expect(
      formatLibrarrSearchStatus({
        state: "ready",
        resultCount: 3,
        mediaType: "audiobook",
      }),
    ).toBe("Found 3 audiobook results.");

    expect(
      formatLibrarrSearchStatus({
        state: "ready",
        resultCount: 0,
        mediaType: "ebook",
      }),
    ).toBe("No ebook results found. Try a title, author, or a shorter search.");
  });
});
