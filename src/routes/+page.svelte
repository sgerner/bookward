<script lang="ts">
  import { applyAction, deserialize, enhance, type SubmitFunction } from "$app/forms";
  import { invalidateAll, pushState } from "$app/navigation";
  import { page } from "$app/state";
  import { onMount, tick, untrack } from "svelte";
  import { flip } from "svelte/animate";
  import { fade, fly, scale, slide } from "svelte/transition";
  import {
    ArrowRight,
    Bell,
    BookOpen,
    Bookmark,
    Check,
    ChevronDown,
    Clock,
    CircleHelp,
    CircleUserRound,
    Compass,
    Copy,
    Database,
    EllipsisVertical,
    ExternalLink,
    History,
    KeyRound,
    Library,
    Link2,
    LogOut,
    Mail,
    MessageCircle,
    RefreshCw,
    Search,
    Send,
    Settings2,
    Sparkles,
    ThumbsDown,
    TriangleAlert,
    Trash2,
    Upload,
    X,
  } from "@lucide/svelte";
  import PopoverDialog from "#lib/components/PopoverDialog.svelte";
  import ProfileField from "#lib/components/ProfileField.svelte";
  import ChipToggle from "#lib/components/ChipToggle.svelte";
  import RadioGroup from "#lib/components/RadioGroup.svelte";
  import SwitchControl from "#lib/components/SwitchControl.svelte";
  import StarRatingBadge from "#lib/components/StarRatingBadge.svelte";
  import ThemePicker from "#lib/components/ThemePicker.svelte";
  import { copyApiTokenText } from "#lib/api-token-clipboard.js";
  import { actionResultMessage } from "#lib/action-result-message.js";
  import { tokenForView } from "#lib/api-token-ui.js";
  import { formatReadingSourceLabel } from "#lib/reading-sources.js";
  import { createTelemetryClient } from "#lib/telemetry.js";
  import {
    buildLibrarrDownloadPayload,
    canRetryLibrarrSearch,
    finalizeLibrarrStreamState,
    librarrFormatChangeAction,
    readLibrarrSearchStream,
    resolveLibrarrResultFormat,
    shouldShowLibrarrEmptyBanner,
  } from "#lib/librarr-stream.js";
  import { modalFocus } from "#lib/actions/modal-focus.js";
  import {
    applyReadingHistoryScores,
    buildViewUrl,
    isSameNavigationUrl,
    READING_HISTORY_SCORE_BATCH_SIZE,
    readUrlViewState,
    reconcileBookMutation,
    reconcilePagedBooks,
    runReadingHistoryScoreBatches,
    sortReadingHistory,
    type DecisionFilter,
    type ReadingHistoryScorePatch,
    type ReadingHistorySort,
    type View,
  } from "#lib/page-state.js";
  import bookwardMark from "#lib/assets/bookward-mark.svg";

  type FilterableView = "discover" | "saved" | "decisions";
  type NavItem = {
    id: View;
    label: string;
    icon: typeof Compass;
    shortLabel: string;
  };
  type FormState = {
    message?: string;
    error?: boolean;
    token?: string;
    id?: number;
    undo_id?: number;
    undo_decisions?: Array<{ id: number; decision_id: number }>;
    undo_label?: string;
  } | null | undefined;
  type MediaType = "ebook" | "audiobook";
  type SourceFilter = "all" | "permanent" | "one_time";
  type ReadingHistoryItem = {
    id: number;
    title: string;
    author: string;
    rating: number | null;
    read_at: string | null;
    source: string;
    created_at: string;
    rank_score: number | null;
    algorithm_score: number | null;
    algorithm_explanation: string[];
    metadata_confidence: number | null;
  };
  type LibrarrResult = Record<string, unknown>;
  type OptimisticChange = {
    commit?: () => void;
    rollback?: () => void;
  };
  type OptimisticFactory = (formData: FormData) => OptimisticChange | void;

  let { data, form } = $props();
  type PageBook = (typeof data.books)[number];
  const initialUrlState = untrack(() =>
    readUrlViewState(
      page.shallow?.url ?? page.url,
      data.digestReview.requested,
    ),
  );
  let activeView = $state<View>(initialUrlState.view);
  let markReadBook = $state<PageBook | null>(null);
  let markReadRating = $state(0);
  let markReadRatingGroup = $state<{ focusFirst: () => void } | null>(null);
  let discoverSearch = $state("");
  let savedSearch = $state("");
  let decisionsSearch = $state("");
  let discoverGenre = $state("");
  let savedGenre = $state("");
  let decisionsGenre = $state("");
  let shortlistSort = $state<"score" | "title" | "author">("score");
  let profileMenuOpen = $state(false);
  let expandedRecommendationDetails = $state(new Set<number>());
  let openReadMenuId = $state<number | null>(null);
  const DISCOVER_PAGE_SIZE = 8;
  const DECISION_PAGE_SIZE = 24;
  const FILTER_DEBOUNCE_MS = 180;
  const FORM_NOTIFICATION_DISMISS_MS = 5000;
  const FORM_NOTIFICATION_ERROR_DISMISS_MS = FORM_NOTIFICATION_DISMISS_MS + 1500;
  let discoverVisibleCount = $state(DISCOVER_PAGE_SIZE);
  let loadMoreSentinel = $state<HTMLElement | null>(null);
  let additionalDiscoverBooks = $state<PageBook[]>([]);
  let unfilteredDiscoverOffset = $state(
    untrack(() => data.books.filter((book) => book.status === "recommended").length),
  );
  let unfilteredDiscoverHasMore = $state(
    untrack(() => Boolean(data.recommendations_has_more)),
  );
  let filteredDiscoverBooks = $state<PageBook[]>([]);
  let filteredDiscoverOffset = $state(0);
  let filteredDiscoverHasMore = $state(false);
  let filteredDiscoverLoadedKey = $state("");
  let filteredDiscoverPending = $state(false);
  let discoverFilterAbortController: AbortController | null = null;
  let discoverFilterDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  let additionalDecisionBooks = $state<PageBook[]>([]);
  let unfilteredDecisionOffset = $state(untrack(() => data.decisions.length));
  let queriedDecisionBooks = $state<PageBook[]>([]);
  let queriedDecisionOffset = $state(0);
  let queriedDecisionsHasMore = $state(false);
  let queriedDecisionLoadedKey = $state("");
  let queriedDecisionPending = $state(false);
  let decisionQueryAbortController: AbortController | null = null;
  let decisionQueryDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  let previousQueriedDecisionStatus = "decisions";
  let discoverLoading = $state(false);
  let discoverLoadError = $state("");
  let decisionsHasMoreOverride = $state<boolean | null>(null);
  let decisionsLoading = $state(false);
  let decisionsLoadError = $state("");
  let decisionFilter = $state<DecisionFilter>(initialUrlState.decisionFilter);
  let readingHistory = $state<ReadingHistoryItem[]>([]);
  let readingHistoryState = $state<"idle" | "loading" | "ready" | "error">("idle");
  let readingHistoryScoreState = $state<"idle" | "loading" | "ready" | "error">("idle");
  let readingHistoryError = $state("");
  let readingHistoryScoreError = $state("");
  let readingHistoryScoreOffset = 0;
  let readingHistoryScoreTask: Promise<void> | null = null;
  let readingHistoryGeneration = 0;
  let bufferedReadingHistoryScores = $state(new Map<number, ReadingHistoryScorePatch>());
  let readingHistoryVisibleCount = $state(30);
  let readingHistoryRatingFilter = $state<"all" | "rated" | "unrated">("all");
  let readingHistorySourceFilter = $state("all");
  let readingHistorySort = $state<ReadingHistorySort>("recent");
  let expandedReadingDetails = $state<number[]>([]);
  // Keep identity sentinels outside `$state` to avoid proxying server arrays
  // and retriggering synchronization effects unnecessarily.
  let previousDataBooks: PageBook[] | null = untrack(() => data.books);
  let previousDataRecommendationsHasMore: boolean | null = untrack(() =>
    Boolean(data.recommendations_has_more),
  );
  let previousDataDecisions: PageBook[] | null = untrack(() => data.decisions);
  let previousDataDecisionsHasMore: boolean | null = untrack(() =>
    Boolean(data.decisions_has_more),
  );
  let previousProfileId: string | undefined = untrack(
    () => data.user?.profile_id,
  );
  let previousDiscoverSearch = "";
  let previousDiscoverGenre = "";
  const undoBookSnapshots = new Map<
    number,
    {
      book: PageBook;
      previousStatus: string;
      discoverIndex: number;
      decisionIndex: number;
    }
  >();
  let pendingActions = $state(new Set<string>());
  let sourceFilter = $state<SourceFilter>("all");
  let librarrSearchOpen = $state(false);
  let librarrSearchBook = $state<{
    id: number;
    title: string;
    author: string;
  } | null>(null);
  let librarrQuery = $state("");
  let librarrMediaType = $state<MediaType>("audiobook");
  let librarrSearchedMediaType = $state<MediaType>("audiobook");
  let librarrResults = $state<LibrarrResult[]>([]);
  let librarrSearchState = $state<"idle" | "searching" | "ready" | "error">(
    "idle",
  );
  let librarrSearchError = $state("");
  let librarrSearchMessage = $state("");
  let librarrAddingKey = $state<string | null>(null);
  let librarrBulkAdding = $state(false);
  let librarrAdded = $state<Set<string>>(new Set());
  let librarrSearchAbortController: AbortController | null = null;
  let digestMode = $state(initialUrlState.digestMode);
  let selectedDigestIds = $state<Set<number>>(new Set());
  let digestEnabledOverride = $state<boolean | null>(null);
  let digestOnlyNewOverride = $state<boolean | null>(null);
  let digestDiscordOverride = $state<boolean | null>(null);
  let digestEmailOverride = $state<boolean | null>(null);
  let apiTokenCopyMessage = $state<string | null>(null);
  let revealedApiToken = $state<string | null>(null);
  let formNotificationDismissed = $state(false);
  let notificationPointerInside = false;
  let notificationFocusInside = false;
  let notificationPaused = false;
  let formNotificationTimer: ReturnType<typeof setTimeout> | null = null;
  let formNotificationDismissDelay = FORM_NOTIFICATION_DISMISS_MS;
  let optimisticNotification = $state<{
    message: string;
    error: boolean;
    id: number;
    undo_id?: number;
    undo_decisions?: Array<{ id: number; decision_id: number }>;
    undo_label?: string;
  } | null>(null);
  let optimisticNotificationId = 0;
  let optimisticNotificationTimer: ReturnType<typeof setTimeout> | null = null;
  let optimisticNotificationDismissDelay = FORM_NOTIFICATION_ERROR_DISMISS_MS;
  let defaultSourceEnabledOverride = $state<boolean | null>(null);
  let sourceEnabledOverrides = $state<Record<number, boolean>>({});
  let sourceScheduleOverride = $state<number | null>(null);
  let librarrUrlOverride = $state<string | null>(null);
  let librarrConnectedOverride = $state<boolean | null>(null);
  let librarrMediaTypeOverride = $state<MediaType | null>(null);
  let embeddingBackendOverride = $state<string | null>(null);
  let embeddingModelOverride = $state<string | null>(null);
  let embeddingUrlOverride = $state<string | null>(null);
  type LlmCatalogProvider = {
    id: string;
    name: string;
    api: string;
    npm: string;
    models: Array<{ id: string; name: string }>;
  };
  let llmCatalogProviders = $state<LlmCatalogProvider[]>([]);
  let llmCatalogLoading = $state(false);
  let llmCatalogError = $state("");
  let llmProviderSearch = $state("");
  let llmModelSearch = $state("");
  let llmEnabledOverride = $state<boolean | null>(null);
  let llmProviderOverride = $state<string | null>(null);
  let llmApiUrlOverride = $state<string | null>(null);
  let llmModelOverride = $state<string | null>(null);
  let revokedApiTokenIds = $state(new Set<number>());
  let bookStatusOverrides = $state<Record<number, string>>({});
  let profileHeaders = $derived({ "x-bookward-profile": data.user?.profile_id ?? "" });
  const telemetry = createTelemetryClient();
  $effect(() => telemetry.setProfileId(data.user?.profile_id));

  const navItems: NavItem[] = [
    {
      id: "discover",
      label: "Discover",
      shortLabel: "Discover",
      icon: Compass,
    },
    { id: "saved", label: "Shortlist", shortLabel: "Saved", icon: Bookmark },
    {
      id: "decisions",
      label: "History",
      shortLabel: "History",
      icon: History,
    },
    { id: "sources", label: "Sources", shortLabel: "Sources", icon: Link2 },
    {
      id: "settings",
      label: "Settings",
      shortLabel: "Settings",
      icon: Settings2,
    },
  ];
  function searchForView(view: FilterableView) {
    if (view === "saved") return savedSearch;
    if (view === "decisions") return decisionsSearch;
    return discoverSearch;
  }
  function setSearchForView(view: FilterableView, value: string) {
    if (view === "saved") savedSearch = value;
    else if (view === "decisions") decisionsSearch = value;
    else discoverSearch = value;
  }
  function genreForView(view: FilterableView) {
    if (view === "saved") return savedGenre;
    if (view === "decisions") return decisionsGenre;
    return discoverGenre;
  }
  function setGenreForView(view: FilterableView, value: string) {
    if (view === "saved") savedGenre = value;
    else if (view === "decisions") decisionsGenre = value;
    else discoverGenre = value;
  }
  function mergeGenres(...lists: ReadonlyArray<ReadonlyArray<string>>) {
    const seen = new Map<string, string>();
    for (const list of lists) {
      for (const raw of list) {
        const cleaned = raw.trim();
        if (cleaned && !seen.has(cleaned.toLowerCase())) {
          seen.set(cleaned.toLowerCase(), cleaned);
        }
      }
    }
    return [...seen.values()].sort((left, right) => left.localeCompare(right));
  }
  function matchesBookFilter(book: PageBook, searchQuery: string, selectedGenre: string) {
    const query = searchQuery.trim().toLowerCase();
    const genre = selectedGenre.trim().toLowerCase();
    return (
      (!query ||
        `${book.title} ${book.author} ${book.genres.join(" ")} ${book.synopsis}`
          .toLowerCase()
          .includes(query)) &&
      (!genre ||
        book.genres.some((item) => item.trim().toLowerCase() === genre))
    );
  }
  function withStatusOverride(book: PageBook): PageBook {
    const status = bookStatusOverrides[book.id];
    return status === undefined ? book : { ...book, status };
  }
  function sourceEnabled(source: { id: number; enabled: number }) {
    return sourceEnabledOverrides[source.id] ?? Boolean(source.enabled);
  }
  const digestSettings = $derived(data.profile.digest);
  const digestVisible = $derived(
    digestMode &&
      (!(page.shallow?.url ?? page.url).searchParams.has("digest_period") ||
        data.digestReview.requested),
  );
  const digestRequested = $derived(
    digestVisible && data.digestReview.requested,
  );
  const allBooks = $derived.by(() => {
    const byId = new Map<number, PageBook>();
    for (const book of [
      ...data.books,
      ...(data.digestReview.books ?? []),
      ...data.decisions,
      ...additionalDiscoverBooks,
      ...filteredDiscoverBooks,
      ...additionalDecisionBooks,
      ...queriedDecisionBooks,
    ]) {
      if (!byId.has(book.id)) {
        byId.set(book.id, withStatusOverride(book));
      }
    }
    return [...byId.values()];
  });
  const activeDigestReviewBooks = $derived.by(() => {
    if (Array.isArray(data.digestReview.books)) {
      return data.digestReview.books
        .map(withStatusOverride)
        .filter((book) => book.status === "recommended");
    }
    return allBooks.filter(
      (book) => book.status === "recommended" && data.digestReview.ids.includes(book.id),
    );
  });
  const unfilteredDiscoverBooks = $derived.by(() => {
    const seen = new Set<number>();
    const items: PageBook[] = [];
    for (const raw of [...data.books, ...additionalDiscoverBooks]) {
      if (seen.has(raw.id)) continue;
      seen.add(raw.id);
      const book = withStatusOverride(raw);
      if (book.status === "recommended") items.push(book);
    }
    return items;
  });
  const hasActiveDiscoverFilter = $derived(
    !digestRequested && Boolean(discoverSearch.trim() || discoverGenre.trim()),
  );
  const activeDiscoverFilterKey = $derived(
    `${discoverSearch.trim().toLowerCase()}\u0000${discoverGenre.trim().toLowerCase()}`,
  );
  const discoverBooks = $derived.by(() => {
    if (!hasActiveDiscoverFilter) return unfilteredDiscoverBooks;
    if (filteredDiscoverLoadedKey === activeDiscoverFilterKey) {
      return filteredDiscoverBooks
        .map(withStatusOverride)
        .filter(
          (book) =>
            book.status === "recommended" &&
            matchesBookFilter(book, discoverSearch, discoverGenre),
        );
    }
    return unfilteredDiscoverBooks.filter((book) =>
      matchesBookFilter(book, discoverSearch, discoverGenre),
    );
  });
  const effectiveDiscoverHasMore = $derived(
    digestRequested
      ? false
      : hasActiveDiscoverFilter
        ? filteredDiscoverLoadedKey === activeDiscoverFilterKey && filteredDiscoverHasMore
        : unfilteredDiscoverHasMore,
  );
  const discoverViewBooks = $derived(
    digestVisible
      ? digestRequested
        ? activeDigestReviewBooks.filter((book) =>
            matchesBookFilter(book, discoverSearch, discoverGenre),
          )
        : discoverBooks.filter((book) => book.score >= digestSettings.minimum_score)
      : discoverBooks,
  );
  const discoverViewVisibleBooks = $derived(
    digestRequested
      ? discoverViewBooks
      : discoverViewBooks.slice(0, discoverVisibleCount),
  );
  const savedBooks = $derived(
    allBooks.filter((book) => book.status === "saved" || book.status === "imported"),
  );
  const shortlistTotalCount = $derived(savedBooks.length);
  const shortlistBooks = $derived.by(() =>
    savedBooks
      .filter((book) => matchesBookFilter(book, savedSearch, savedGenre))
      .sort((left, right) => {
        if (shortlistSort === "title") return left.title.localeCompare(right.title);
        if (shortlistSort === "author") return left.author.localeCompare(right.author) || left.title.localeCompare(right.title);
        return right.score - left.score || left.title.localeCompare(right.title);
      }),
  );
  const isQueriedDecisionMode = $derived(
    decisionFilter !== "read" &&
      (decisionFilter !== "all" ||
        Boolean(decisionsSearch.trim() || decisionsGenre.trim())),
  );
  const activeDecisionApiStatus = $derived(
    decisionFilter === "maybe_later" || decisionFilter === "rejected"
      ? decisionFilter
      : "decisions",
  );
  const activeDecisionQueryKey = $derived(
    `${activeDecisionApiStatus}\u0000${decisionsSearch.trim().toLowerCase()}\u0000${decisionsGenre.trim().toLowerCase()}`,
  );
  const unfilteredDecisionBooks = $derived.by(() => {
    const decisionIdSet = new Set(
      [...data.decisions, ...additionalDecisionBooks].map((book) => book.id),
    );
    const optimisticNewDecisions = allBooks.filter(
      (book) =>
        !decisionIdSet.has(book.id) &&
        ["rejected", "maybe_later"].includes(book.status),
    );
    const seen = new Set<number>(optimisticNewDecisions.map((book) => book.id));
    const ordered = [...optimisticNewDecisions];
    for (const raw of [...data.decisions, ...additionalDecisionBooks]) {
      if (seen.has(raw.id)) continue;
      seen.add(raw.id);
      const book = withStatusOverride(raw);
      if (["rejected", "maybe_later"].includes(book.status)) {
        ordered.push(book);
      }
    }
    return ordered;
  });
  const decisionBooks = $derived.by(() => {
    if (!isQueriedDecisionMode) return unfilteredDecisionBooks;
    const matchesDecisionTab = (book: PageBook) =>
      ["rejected", "maybe_later"].includes(book.status) &&
      (decisionFilter === "all" || book.status === decisionFilter) &&
      matchesBookFilter(book, decisionsSearch, decisionsGenre);
    if (queriedDecisionLoadedKey === activeDecisionQueryKey) {
      const queriedIdSet = new Set(queriedDecisionBooks.map((book) => book.id));
      const optimisticMatches = allBooks.filter(
        (book) =>
          bookStatusOverrides[book.id] !== undefined &&
          !queriedIdSet.has(book.id) &&
          matchesDecisionTab(book),
      );
      return [
        ...optimisticMatches,
        ...queriedDecisionBooks.map(withStatusOverride).filter(matchesDecisionTab),
      ];
    }
    return unfilteredDecisionBooks.filter(matchesDecisionTab);
  });
  const decisionsHasMore = $derived(
    isQueriedDecisionMode
      ? queriedDecisionLoadedKey === activeDecisionQueryKey && queriedDecisionsHasMore
      : decisionsHasMoreOverride ?? Boolean(data.decisions_has_more),
  );
  const discoverGenres = $derived(
    digestRequested
      ? mergeGenres(...activeDigestReviewBooks.map((book) => book.genres))
      : mergeGenres(
          data.recommendation_genres ?? [],
          ...unfilteredDiscoverBooks.map((book) => book.genres),
          ...filteredDiscoverBooks.map((book) => book.genres),
        ),
  );
  const savedGenres = $derived(
    mergeGenres(...savedBooks.map((book) => book.genres)),
  );
  const decisionGenres = $derived(
    decisionFilter === "read"
      ? []
      : mergeGenres(
          data.decision_genres ?? [],
          ...data.decisions.map((book) => book.genres),
          ...additionalDecisionBooks.map((book) => book.genres),
          ...queriedDecisionBooks.map((book) => book.genres),
        ),
  );
  function genresForView(view: FilterableView) {
    if (view === "saved") return savedGenres;
    if (view === "decisions") return decisionGenres;
    return discoverGenres;
  }
  function canonicalGenreForView(view: FilterableView, rawGenre: string) {
    const target = rawGenre.trim().toLowerCase();
    return (
      genresForView(view).find((genre) => genre.toLowerCase() === target) ??
      rawGenre.trim()
    );
  }
  const filteredReadingHistory = $derived.by(() => {
    const query = decisionsSearch.trim().toLowerCase();
    const matchesRating = (item: ReadingHistoryItem) => {
      if (readingHistoryRatingFilter === "rated") return item.rating !== null && item.rating > 0;
      if (readingHistoryRatingFilter === "unrated") return item.rating === null || item.rating <= 0;
      return true;
    };
    const matchesSearch = (item: ReadingHistoryItem) =>
      !query || `${item.title} ${item.author} ${item.source}`.toLowerCase().includes(query);
    const matching = readingHistory.filter(
      (item) =>
        matchesSearch(item) &&
        matchesRating(item) &&
        (readingHistorySourceFilter === "all" || item.source === readingHistorySourceFilter),
    );
    return sortReadingHistory(
      matching,
      readingHistorySort,
      readingHistoryScoreState === "idle" || readingHistoryScoreState === "loading",
    );
  });
  const readingHistorySources = $derived(
    [...new Set(readingHistory.map((item) => item.source).filter(Boolean))]
      .sort((left, right) => readingSourceLabel(left).localeCompare(readingSourceLabel(right))),
  );
  const visibleReadingHistory = $derived(
    filteredReadingHistory.slice(0, readingHistoryVisibleCount),
  );
  const scoredReadingHistoryCount = $derived(
    readingHistory.filter(
      (item) => item.algorithm_score !== null || bufferedReadingHistoryScores.has(item.id),
    ).length,
  );
  const selectedDigestCount = $derived(selectedDigestIds.size);
  const selectedDigestRunId = $derived.by(() => {
    if (!selectedDigestIds.size) return "";
    const booksById = new Map(allBooks.map((book) => [book.id, book]));
    let sharedRunId = "";
    for (const id of selectedDigestIds) {
      const runId = booksById.get(id)?.recommendation_run_id?.trim() ?? "";
      if (runId.length < 8) return "";
      if (!sharedRunId) sharedRunId = runId;
      else if (sharedRunId !== runId) return "";
    }
    return sharedRunId;
  });
  const defaultSourceEnabled = $derived(
    defaultSourceEnabledOverride ?? Boolean(data.profile.default_source_enabled),
  );
  const sourceSyncIntervalHours = $derived(
    sourceScheduleOverride ?? data.profile.source_sync_interval_hours,
  );
  const librarrUrl = $derived(librarrUrlOverride ?? data.profile.librar_url);
  const librarrConnected = $derived(
    librarrConnectedOverride ?? Boolean(data.profile.librar_connected),
  );
  const configuredLibrarrMediaType = $derived(
    librarrMediaTypeOverride ?? data.profile.librarr_media_type,
  );
  const embeddingBackend = $derived(
    embeddingBackendOverride ?? data.profile.embedding_backend,
  );
  const embeddingModel = $derived(
    embeddingModelOverride ?? data.profile.embedding_model,
  );
  const embeddingUrl = $derived(
    embeddingUrlOverride ?? data.profile.embedding_url,
  );
  const llmRerank = $derived(
    data.profile.llm_rerank ?? {
      enabled: false,
      provider: "",
      api_url: "https://api.openai.com/v1/chat/completions",
      model: "gpt-4o-mini",
      api_key_set: false,
    },
  );
  const llmEnabled = $derived(llmEnabledOverride ?? llmRerank.enabled);
  const llmProvider = $derived(llmProviderOverride ?? llmRerank.provider);
  const llmApiUrl = $derived(llmApiUrlOverride ?? llmRerank.api_url);
  const llmModel = $derived(llmModelOverride ?? llmRerank.model);
  const filteredLlmProviders = $derived.by(() => {
    const q = llmProviderSearch.trim().toLowerCase();
    if (!q) return llmCatalogProviders;
    return llmCatalogProviders.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.id.toLowerCase().includes(q),
    );
  });
  const selectedCatalogProvider = $derived(
    llmCatalogProviders.find((p) => p.id === llmProvider) ?? null,
  );
  const filteredLlmModels = $derived.by(() => {
    const models = selectedCatalogProvider?.models ?? [];
    const q = llmModelSearch.trim().toLowerCase();
    if (!q) return models;
    return models.filter(
      (m) =>
        m.name.toLowerCase().includes(q) ||
        m.id.toLowerCase().includes(q),
    );
  });

  async function loadLlmCatalog() {
    if (llmCatalogLoading) return;
    llmCatalogLoading = true;
    llmCatalogError = "";
    try {
      const response = await fetch("/api/llm/models");
      const body = await response.json();
      if (!response.ok) {
        llmCatalogError = body?.message || "Could not load models.dev catalog.";
        return;
      }
      llmCatalogProviders = Array.isArray(body?.providers) ? body.providers : [];
    } catch (error) {
      llmCatalogError =
        error instanceof Error
          ? error.message
          : "Could not load models.dev catalog.";
    } finally {
      llmCatalogLoading = false;
    }
  }

  function handleLlmProviderChange(providerId: string) {
    llmProviderOverride = providerId;
    llmModelSearch = "";
    const match = llmCatalogProviders.find((p) => p.id === providerId);
    if (!match) return;
    if (match.api) {
      llmApiUrlOverride = match.api;
    }
    if (match.models.length > 0) {
      llmModelOverride = match.models[0].id;
    }
  }
  const apiTokens = $derived(
    data.profile.api_tokens.filter((token) => !revokedApiTokenIds.has(token.id)),
  );
  const digestEnabled = $derived(
    digestEnabledOverride ?? digestSettings.enabled,
  );
  const digestOnlyNew = $derived(
    digestOnlyNewOverride ?? digestSettings.only_new,
  );
  const digestDiscord = $derived(
    digestDiscordOverride ?? digestSettings.channels.includes("discord"),
  );
  const digestEmail = $derived(
    digestEmailOverride ?? digestSettings.channels.includes("email"),
  );
  const hasMoreDiscoverBooks = $derived(
    activeView === "discover" &&
      (discoverViewVisibleBooks.length < discoverViewBooks.length ||
        effectiveDiscoverHasMore),
  );
  const activeSourceCount = $derived(
    data.sources.filter(sourceEnabled).length + (defaultSourceEnabled ? 1 : 0),
  );
  const permanentSources = $derived(
    data.sources.filter((source) => source.lifecycle !== "one_time"),
  );
  const oneTimeSources = $derived(
    data.sources.filter((source) => source.lifecycle === "one_time"),
  );
  const activePermanentSourceCount = $derived(
    permanentSources.filter(sourceEnabled).length,
  );
  const activeOneTimeSourceCount = $derived(
    oneTimeSources.filter(sourceEnabled).length,
  );
  const sourceGroups = $derived([
    {
      key: "permanent" as const,
      label: "Permanent feeds",
      description: "Reviewed automatically on your cadence.",
      active: activePermanentSourceCount,
      sources: permanentSources,
    },
    {
      key: "one_time" as const,
      label: "One-time imports",
      description: "Scanned once, then kept for discovery.",
      active: activeOneTimeSourceCount,
      sources: oneTimeSources,
    },
  ]);
  const visibleSourceGroups = $derived(
    sourceFilter === "all"
      ? sourceGroups
      : sourceGroups.filter((group) => group.key === sourceFilter),
  );
  const sourceFilterOptions = $derived([
    {
      value: "all" as const,
      label: "All",
      count: permanentSources.length + oneTimeSources.length,
    },
    {
      value: "permanent" as const,
      label: "Permanent",
      count: permanentSources.length,
    },
    {
      value: "one_time" as const,
      label: "One-time",
      count: oneTimeSources.length,
    },
  ]);
  const formState = $derived((form ?? null) as FormState);
  const visibleNotification = $derived(
    optimisticNotification ??
      (formState?.message && !formNotificationDismissed ? formState : null),
  );
  const formIsError = $derived(Boolean(visibleNotification?.error));

  function scheduleFormNotificationDismiss() {
    if (formNotificationTimer) clearTimeout(formNotificationTimer);
    formNotificationTimer = null;
    if (untrack(() => notificationPaused || Boolean(optimisticNotification))) return;
    if (formNotificationDismissDelay === 0) return;
    formNotificationTimer = setTimeout(() => {
      formNotificationDismissed = true;
      formNotificationTimer = null;
    }, formNotificationDismissDelay);
  }

  function scheduleOptimisticNotificationDismiss() {
    if (optimisticNotificationTimer) clearTimeout(optimisticNotificationTimer);
    optimisticNotificationTimer = null;
    const id = optimisticNotification?.id;
    if (id === undefined || notificationPaused) return;
    optimisticNotificationTimer = setTimeout(() => {
      if (optimisticNotification?.id !== id) return;
      optimisticNotification = null;
      formNotificationDismissed = true;
      optimisticNotificationTimer = null;
    }, optimisticNotificationDismissDelay);
  }

  function updateNotificationPause() {
    const shouldPause = notificationPointerInside || notificationFocusInside;
    if (shouldPause === notificationPaused) return;
    notificationPaused = shouldPause;
    if (notificationPaused) {
      if (formNotificationTimer) clearTimeout(formNotificationTimer);
      if (optimisticNotificationTimer) clearTimeout(optimisticNotificationTimer);
      formNotificationTimer = null;
      optimisticNotificationTimer = null;
    } else if (optimisticNotification) {
      scheduleOptimisticNotificationDismiss();
    } else if (formState?.message && !formNotificationDismissed) {
      scheduleFormNotificationDismiss();
    }
  }

  function onNotificationFocusOut(event: FocusEvent) {
    const container = event.currentTarget as HTMLElement;
    if (event.relatedTarget instanceof Node && container.contains(event.relatedTarget)) return;
    notificationFocusInside = false;
    updateNotificationPause();
  }

  $effect(() => {
    // Read the prop itself so identical messages from separate submissions
    // still restart the notification timer.
    const currentForm = form as FormState;
    const message = currentForm?.message;
    formNotificationDismissed = false;
    if (!message) return;

    formNotificationDismissDelay = currentForm?.error
      ? FORM_NOTIFICATION_ERROR_DISMISS_MS
      : currentForm?.undo_id || currentForm?.undo_decisions?.length
        ? 0
        : FORM_NOTIFICATION_DISMISS_MS;
    scheduleFormNotificationDismiss();
    return () => {
      if (formNotificationTimer) clearTimeout(formNotificationTimer);
      formNotificationTimer = null;
    };
  });

  function resetClientPagination() {
    discoverFilterAbortController?.abort();
    discoverFilterAbortController = null;
    decisionQueryAbortController?.abort();
    decisionQueryAbortController = null;
    additionalDiscoverBooks = [];
    unfilteredDiscoverOffset = data.books.filter(
      (book) => book.status === "recommended",
    ).length;
    unfilteredDiscoverHasMore = Boolean(data.recommendations_has_more);
    filteredDiscoverBooks = [];
    filteredDiscoverOffset = 0;
    filteredDiscoverHasMore = false;
    filteredDiscoverLoadedKey = "";
    filteredDiscoverPending = false;
    discoverLoading = false;
    discoverLoadError = "";
    discoverVisibleCount = DISCOVER_PAGE_SIZE;
    additionalDecisionBooks = [];
    unfilteredDecisionOffset = data.decisions.length;
    queriedDecisionBooks = [];
    queriedDecisionOffset = 0;
    queriedDecisionsHasMore = false;
    queriedDecisionLoadedKey = "";
    queriedDecisionPending = false;
    decisionsHasMoreOverride = null;
    decisionsLoading = false;
    decisionsLoadError = "";
    undoBookSnapshots.clear();
  }

  function resetReadingHistory() {
    readingHistoryGeneration += 1;
    readingHistoryScoreTask = null;
    readingHistory = [];
    readingHistoryState = "idle";
    readingHistoryError = "";
    readingHistoryScoreState = "idle";
    readingHistoryScoreError = "";
    readingHistoryScoreOffset = 0;
    bufferedReadingHistoryScores = new Map();
  }

  async function loadReadingHistory() {
    if (readingHistoryState === "loading" || readingHistoryState === "ready") return;
    const generation = ++readingHistoryGeneration;
    readingHistoryScoreTask = null;
    readingHistoryState = "loading";
    readingHistoryError = "";
    readingHistoryScoreState = "idle";
    readingHistoryScoreError = "";
    readingHistoryScoreOffset = 0;
    bufferedReadingHistoryScores = new Map();
    try {
      const response = await fetch("/api/reading-history", { headers: profileHeaders });
      const payload = (await response.json().catch(() => ({}))) as {
        items?: ReadingHistoryItem[];
        message?: string;
      };
      if (generation !== readingHistoryGeneration) return;
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Reading history could not be loaded.");
      }
      readingHistory = payload.items;
      readingHistoryState = "ready";
      void loadReadingHistoryScores(payload.items.length, generation);
    } catch (error) {
      if (generation !== readingHistoryGeneration) return;
      readingHistoryError = error instanceof Error
        ? error.message
        : "Reading history could not be loaded.";
      readingHistoryState = "error";
    }
  }
  function loadReadingHistoryScores(total: number, generation = readingHistoryGeneration) {
    if (readingHistoryScoreTask) return readingHistoryScoreTask;
    const batchSize = READING_HISTORY_SCORE_BATCH_SIZE;
    if (total === 0 || readingHistoryScoreOffset >= total) {
      if (bufferedReadingHistoryScores.size > 0) {
        readingHistory = applyReadingHistoryScores(
          readingHistory,
          bufferedReadingHistoryScores,
        );
        bufferedReadingHistoryScores = new Map();
      }
      readingHistoryScoreState = "ready";
      return Promise.resolve();
    }
    readingHistoryScoreState = "loading";
    readingHistoryScoreError = "";
    let finished = false;
    let task: Promise<void> | null = null;
    const run = async () => {
      try {
        const outcome = await runReadingHistoryScoreBatches({
          total,
          startOffset: readingHistoryScoreOffset,
          initialBuffer: bufferedReadingHistoryScores,
          batchSize,
          isCurrent: () => generation === readingHistoryGeneration,
          fetchBatch: async (offset, limit) => {
            const response = await fetch(
              `/api/reading-history/scores?offset=${offset}&limit=${limit}`,
              { headers: profileHeaders },
            );
            const payload = (await response.json().catch(() => ({}))) as {
              scores?: ReadingHistoryScorePatch[];
              message?: string;
            };
            return {
              ok: response.ok,
              status: response.status,
              scores: payload.scores,
              message: payload.message,
            };
          },
          onBatch: ({ nextOffset, buffered }) => {
            bufferedReadingHistoryScores = buffered;
            readingHistoryScoreOffset = nextOffset;
          },
        });
        if (outcome.status === "stale") return;
        if (outcome.status === "error") {
          readingHistoryScoreOffset = outcome.nextOffset;
          bufferedReadingHistoryScores = outcome.buffered;
          readingHistoryScoreError = outcome.message;
          readingHistoryScoreState = "error";
          return;
        }
        readingHistoryScoreOffset = outcome.nextOffset;
        readingHistory = applyReadingHistoryScores(
          readingHistory,
          outcome.buffered,
        );
        bufferedReadingHistoryScores = new Map();
        readingHistoryScoreState = "ready";
      } catch (error) {
        if (generation !== readingHistoryGeneration) return;
        readingHistoryScoreError = error instanceof Error
          ? error.message
          : "Reading history scores could not be loaded.";
        readingHistoryScoreState = "error";
      } finally {
        finished = true;
        if (task && readingHistoryScoreTask === task) {
          readingHistoryScoreTask = null;
        }
      }
    };
    task = run();
    if (!finished) {
      readingHistoryScoreTask = task;
    }
    return task;
  }
  function syncFromUrl(url: Parameters<typeof readUrlViewState>[0]) {
    const nextUrlState = readUrlViewState(url, data.digestReview.requested);
    const previousView = untrack(() => activeView);
    const previousFilter = untrack(() => decisionFilter);
    const previousDigest = untrack(() => digestMode);

    revealedApiToken = tokenForView(
      nextUrlState.view,
      untrack(() => revealedApiToken),
    );
    if (previousView !== nextUrlState.view) {
      activeView = nextUrlState.view;
      if (typeof window !== "undefined") {
        window.scrollTo(0, 0);
      }
    }
    if (previousFilter !== nextUrlState.decisionFilter) {
      decisionFilter = nextUrlState.decisionFilter;
      readingHistoryVisibleCount = 30;
      decisionsLoadError = "";
    }
    if (previousDigest !== nextUrlState.digestMode) {
      digestMode = nextUrlState.digestMode;
      if (!nextUrlState.digestMode) {
        clearDigestSelection();
      }
    }
  }
  function selectDecisionFilter(nextFilter: DecisionFilter) {
    const filterChanged = decisionFilter !== nextFilter;
    decisionFilter = nextFilter;
    if (filterChanged) {
      readingHistoryVisibleCount = 30;
    }
    decisionsLoadError = "";
    if (nextFilter === "read") void loadReadingHistory();
    if (typeof window !== "undefined" && activeView === "decisions") {
      const currentUrl = new URL(window.location.href);
      const nextUrl = buildViewUrl(currentUrl, {
        view: "decisions",
        decisionFilter: nextFilter,
      });
      if (!isSameNavigationUrl(currentUrl, nextUrl)) {
        pushState(nextUrl, {});
      }
    }
  }
  function toggleReadingDetails(id: number) {
    expandedReadingDetails = expandedReadingDetails.includes(id)
      ? expandedReadingDetails.filter((itemId) => itemId !== id)
      : [...expandedReadingDetails, id];
  }
  function toggleRecommendationDetails(id: number) {
    const next = new Set(expandedRecommendationDetails);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    expandedRecommendationDetails = next;
  }
  function readingSourceLabel(source: string) {
    return formatReadingSourceLabel(source);
  }
  function formatHistoryDate(value: string | null) {
    if (!value) return "Not provided";
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? value
      : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  function go(view: View, nextDecisionFilter?: DecisionFilter) {
    const targetFilter =
      view === "decisions"
        ? (nextDecisionFilter ?? (activeView === "decisions" ? decisionFilter : "all"))
        : "all";
    if (
      view === activeView &&
      (nextDecisionFilter === undefined || targetFilter === decisionFilter)
    ) {
      return;
    }
    const viewChanged = view !== activeView;
    revealedApiToken = tokenForView(view, revealedApiToken);
    activeView = view;
    decisionFilter = targetFilter;
    if (view !== "discover" && digestMode) {
      digestMode = false;
      clearDigestSelection();
    }
    if (view === "decisions") {
      readingHistoryVisibleCount = 30;
      if (targetFilter === "read") void loadReadingHistory();
    }
    if (typeof window !== "undefined") {
      const currentUrl = new URL(window.location.href);
      const nextUrl = buildViewUrl(currentUrl, {
        view,
        decisionFilter: targetFilter,
        digestMode: view === "discover" && digestMode,
        clearDigestPeriod: view !== "discover" || !digestMode,
      });
      if (!isSameNavigationUrl(currentUrl, nextUrl)) {
        pushState(nextUrl, {});
      }
      if (viewChanged) {
        window.scrollTo(0, 0);
      }
    }
  }
  function onHomeClick(event: MouseEvent) {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    revealedApiToken = tokenForView("discover", revealedApiToken);
    activeView = "discover";
    decisionFilter = "all";
    if (digestMode) {
      digestMode = false;
      clearDigestSelection();
    }
    if (typeof window !== "undefined") {
      const currentUrl = new URL(window.location.href);
      const nextUrl = new URL("/", currentUrl.origin);
      if (!isSameNavigationUrl(currentUrl, nextUrl)) {
        pushState(nextUrl, {});
      }
      window.scrollTo(0, 0);
    }
  }
  async function goToLibrarrSettings() {
    go("settings");
    await tick();
    const section = document.getElementById("librarr-settings");
    if (!section) return;
    section.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
      block: "start",
    });
    section.focus({ preventScroll: true });
  }
  function announceActionError(message: string) {
    if (formNotificationTimer) clearTimeout(formNotificationTimer);
    formNotificationTimer = null;
    const id = ++optimisticNotificationId;
    formNotificationDismissed = true;
    optimisticNotification = { message, error: true, id };
    optimisticNotificationDismissDelay = FORM_NOTIFICATION_ERROR_DISMISS_MS;
    scheduleOptimisticNotificationDismiss();
  }

  function announceActionSuccess(message: string) {
    if (formNotificationTimer) clearTimeout(formNotificationTimer);
    formNotificationTimer = null;
    const id = ++optimisticNotificationId;
    formNotificationDismissed = true;
    optimisticNotification = { message, error: false, id };
    optimisticNotificationDismissDelay = FORM_NOTIFICATION_DISMISS_MS;
    scheduleOptimisticNotificationDismiss();
  }

  function dismissNotification() {
    if (formNotificationTimer) clearTimeout(formNotificationTimer);
    formNotificationTimer = null;
    if (optimisticNotificationTimer) clearTimeout(optimisticNotificationTimer);
    optimisticNotificationTimer = null;
    optimisticNotification = null;
    formNotificationDismissed = true;
  }

  function withPending(key: string, add: boolean) {
    const next = new Set(pendingActions);
    if (add) next.add(key);
    else next.delete(key);
    pendingActions = next;
  }

  function setPending(
    key: string,
    optimistic?: OptimisticFactory,
  ): SubmitFunction {
    return ({ formData }) => {
      withPending(key, true);
      let change: OptimisticChange | void;
      try {
        change = optimistic?.(formData);
      } catch {
        change = undefined;
      }
      return async ({ update, result }) => {
        const succeeded = result.type === "success" || result.type === "redirect";
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
          if (optimisticNotificationTimer) clearTimeout(optimisticNotificationTimer);
          optimisticNotificationTimer = null;
          optimisticNotification = null;
          await update({ navigate: false });
          if (succeeded && key === "sync") {
            resetClientPagination();
          }
          if (
            succeeded &&
            (key.startsWith("read-") ||
              key.startsWith("shortlist-read-") ||
              key === "goodreads-rss" ||
              key === "csv-import" ||
              key === "goodreads-csv")
          ) {
            resetReadingHistory();
          }
          if (succeeded) change?.commit?.();
          if (
            succeeded &&
            (key === "sync" ||
              key === "rebuild-embeddings" ||
              key === "goodreads-rss" ||
              key === "goodreads-csv" ||
              key === "source" ||
              key.startsWith("source-"))
          ) {
            if (hasActiveDiscoverFilter) reloadFilteredDiscover();
            if (isQueriedDecisionMode) reloadQueriedDecisions();
          }
        } catch (error) {
          rollback();
          announceActionError(
            error instanceof Error
              ? error.message
              : "The request could not be completed. Your change was reverted.",
          );
        } finally {
          withPending(key, false);
        }
      };
    };
  }

  function setPendingDigestBulk(): SubmitFunction {
    return setPending("shortlist-bulk", optimisticShortlistBulk);
  }

  function isPending(key: string) {
    return pendingActions.has(key);
  }

  function applyCommittedStatusToClientSlices(
    id: number,
    status: string,
    fallbackBook?: PageBook,
  ) {
    const knownBook = allBooks.find((item) => item.id === id) ?? fallbackBook;
    const prevFilteredDiscover = filteredDiscoverBooks.find((item) => item.id === id);
    if (
      prevFilteredDiscover &&
      prevFilteredDiscover.status === "recommended" &&
      status !== "recommended"
    ) {
      filteredDiscoverOffset = Math.max(0, filteredDiscoverOffset - 1);
    } else if (
      prevFilteredDiscover &&
      prevFilteredDiscover.status !== "recommended" &&
      status === "recommended"
    ) {
      filteredDiscoverOffset += 1;
    }
    const prevQueriedDecision = queriedDecisionBooks.find((item) => item.id === id);
    const wasDecision = Boolean(
      prevQueriedDecision &&
        ["rejected", "maybe_later"].includes(prevQueriedDecision.status),
    );
    const isDecision = ["rejected", "maybe_later"].includes(status);
    if (wasDecision && !isDecision) {
      queriedDecisionOffset = Math.max(0, queriedDecisionOffset - 1);
    } else if (!wasDecision && isDecision && prevQueriedDecision) {
      queriedDecisionOffset += 1;
    }
    const updateSlice = (items: PageBook[]) =>
      items.some((item) => item.id === id)
        ? items.map((item) => (item.id === id ? { ...item, status } : item))
        : items;
    filteredDiscoverBooks = updateSlice(filteredDiscoverBooks);
    if (
      isDecision &&
      !prevQueriedDecision &&
      queriedDecisionLoadedKey !== "" &&
      knownBook &&
      (decisionFilter === "all" || decisionFilter === status) &&
      matchesBookFilter(knownBook, decisionsSearch, decisionsGenre)
    ) {
      queriedDecisionBooks = [{ ...knownBook, status }, ...queriedDecisionBooks];
      queriedDecisionOffset += 1;
    } else {
      queriedDecisionBooks = updateSlice(queriedDecisionBooks);
    }
  }

  function optimisticBookStatus(
    id: number,
    status: string,
    recordUndo = false,
  ): OptimisticChange | void {
    const discoverIndex = additionalDiscoverBooks.findIndex((item) => item.id === id);
    const decisionIndex = additionalDecisionBooks.findIndex((item) => item.id === id);
    const book = allBooks.find((item) => item.id === id);
    if (!book) return;
    if (recordUndo && ["saved", "rejected", "maybe_later"].includes(status)) {
      undoBookSnapshots.set(id, {
        book: { ...book },
        previousStatus: book.status,
        discoverIndex,
        decisionIndex,
      });
    } else if (status === "recommended" || status === "read") {
      undoBookSnapshots.delete(id);
    }
    const snapshot = { ...book };
    const previous = bookStatusOverrides[id];
    bookStatusOverrides = { ...bookStatusOverrides, [id]: status };
    return {
      commit: () => {
        const reconciled = reconcileBookMutation({
          serverBooks: data.books,
          serverDecisions: data.decisions,
          additionalDiscoverBooks,
          additionalDecisionBooks,
          id,
          nextStatus: status,
          snapshot,
        });
        additionalDiscoverBooks = reconciled.additionalDiscoverBooks;
        additionalDecisionBooks = reconciled.additionalDecisionBooks;
        unfilteredDiscoverOffset =
          data.books.filter((item) => item.status === "recommended").length +
          reconciled.additionalDiscoverBooks.length;
        unfilteredDecisionOffset =
          data.decisions.length + reconciled.additionalDecisionBooks.length;
        if (bookStatusOverrides[id] !== status) return;
        applyCommittedStatusToClientSlices(id, status, snapshot);
        const next = { ...bookStatusOverrides };
        delete next[id];
        bookStatusOverrides = next;
      },
      rollback: () => {
        if (recordUndo) undoBookSnapshots.delete(id);
        if (bookStatusOverrides[id] !== status) return;
        const next = { ...bookStatusOverrides };
        if (previous === undefined) delete next[id];
        else next[id] = previous;
        bookStatusOverrides = next;
      },
    };
  }

  function optimisticDecision(formData: FormData) {
    const id = Number(formData.get("id"));
    const status = String(formData.get("status") ?? "");
    return Number.isInteger(id) && ["saved", "rejected", "maybe_later", "recommended"].includes(status)
      ? optimisticBookStatus(id, status, true)
      : undefined;
  }

  function optimisticUndoDecision(formData: FormData): OptimisticChange | void {
    const ids = [
      ...new Set(
        formData
          .getAll("id")
          .map((value) => Number(value))
          .filter((id) => Number.isInteger(id)),
      ),
    ];
    if (!ids.length) return;
    const entries = ids.map((id) => {
      const undoSnapshot = undoBookSnapshots.get(id);
      const restoredStatus = undoSnapshot?.previousStatus ?? "recommended";
      const fallbackBook = allBooks.find((item) => item.id === id);
      const snapshot =
        undoSnapshot?.book ?? (fallbackBook ? { ...fallbackBook } : undefined);
      const previousOverride = bookStatusOverrides[id];
      return { id, undoSnapshot, restoredStatus, snapshot, previousOverride };
    });
    const nextOverrides = { ...bookStatusOverrides };
    for (const entry of entries) {
      nextOverrides[entry.id] = entry.restoredStatus;
    }
    bookStatusOverrides = nextOverrides;
    return {
      commit: () => {
        let nextDiscover = additionalDiscoverBooks;
        let nextDecisions = additionalDecisionBooks;
        for (const entry of entries) {
          const reconciled = reconcileBookMutation({
            serverBooks: data.books,
            serverDecisions: data.decisions,
            additionalDiscoverBooks: nextDiscover,
            additionalDecisionBooks: nextDecisions,
            id: entry.id,
            nextStatus: entry.restoredStatus,
            snapshot: entry.snapshot,
            restoreDiscoverIndex: entry.undoSnapshot?.discoverIndex,
            restoreDecisionIndex: entry.undoSnapshot?.decisionIndex,
          });
          nextDiscover = reconciled.additionalDiscoverBooks;
          nextDecisions = reconciled.additionalDecisionBooks;
          undoBookSnapshots.delete(entry.id);
          applyCommittedStatusToClientSlices(
            entry.id,
            entry.restoredStatus,
            entry.snapshot,
          );
        }
        additionalDiscoverBooks = nextDiscover;
        additionalDecisionBooks = nextDecisions;
        unfilteredDiscoverOffset =
          data.books.filter((item) => item.status === "recommended").length +
          nextDiscover.length;
        unfilteredDecisionOffset = data.decisions.length + nextDecisions.length;
        const cleaned = { ...bookStatusOverrides };
        for (const entry of entries) {
          if (cleaned[entry.id] === entry.restoredStatus) {
            delete cleaned[entry.id];
          }
        }
        bookStatusOverrides = cleaned;
      },
      rollback: () => {
        const reverted = { ...bookStatusOverrides };
        for (const entry of entries) {
          if (reverted[entry.id] !== entry.restoredStatus) continue;
          if (entry.previousOverride === undefined) delete reverted[entry.id];
          else reverted[entry.id] = entry.previousOverride;
        }
        bookStatusOverrides = reverted;
      },
    };
  }

  function optimisticRead(formData: FormData) {
    const id = Number(formData.get("id"));
    return Number.isInteger(id) ? optimisticBookStatus(id, "read") : undefined;
  }

  function optimisticShortlistRead(formData: FormData) {
    const change = optimisticRead(formData);
    return {
      commit: () => {
        change?.commit?.();
        closeMarkRead();
      },
      rollback: () => change?.rollback?.(),
    };
  }

  function optimisticImport(formData: FormData) {
    const id = Number(formData.get("id"));
    return Number.isInteger(id) ? optimisticBookStatus(id, "imported") : undefined;
  }

  function optimisticShortlistBulk(formData: FormData) {
    const ids = [...new Set(
      formData
        .getAll("ids")
        .map((value) => Number(value))
        .filter((id) => Number.isInteger(id)),
    )];
    const changes = ids.map((id) => optimisticBookStatus(id, "saved", true)).filter(Boolean) as OptimisticChange[];
    return {
      commit: () => {
        changes.forEach((change) => change.commit?.());
        clearDigestSelection();
      },
      rollback: () => changes.forEach((change) => change.rollback?.()),
    };
  }

  function optimisticSourceToggle(formData: FormData) {
    const id = Number(formData.get("id"));
    const source = data.sources.find((item) => item.id === id);
    if (!source) return;
    const previous = sourceEnabledOverrides[id];
    const nextValue = !(previous ?? Boolean(source.enabled));
    sourceEnabledOverrides = { ...sourceEnabledOverrides, [id]: nextValue };
    return {
      commit: () => {
        if (sourceEnabledOverrides[id] !== nextValue) return;
        const next = { ...sourceEnabledOverrides };
        delete next[id];
        sourceEnabledOverrides = next;
      },
      rollback: () => {
        if (sourceEnabledOverrides[id] !== nextValue) return;
        const next = { ...sourceEnabledOverrides };
        if (previous === undefined) delete next[id];
        else next[id] = previous;
        sourceEnabledOverrides = next;
      },
    };
  }

  function optimisticDefaultToggle() {
    const previous = defaultSourceEnabledOverride;
    const nextValue = !defaultSourceEnabled;
    defaultSourceEnabledOverride = nextValue;
    return {
      commit: () => {
        if (defaultSourceEnabledOverride === nextValue) defaultSourceEnabledOverride = null;
      },
      rollback: () => {
        if (defaultSourceEnabledOverride === nextValue) defaultSourceEnabledOverride = previous;
      },
    };
  }

  function optimisticSourceSchedule(formData: FormData) {
    const nextValue = Number(formData.get("intervalHours"));
    if (!Number.isInteger(nextValue)) return;
    sourceScheduleOverride = nextValue;
    return {
      commit: () => {
        if (sourceScheduleOverride === nextValue) sourceScheduleOverride = null;
      },
      rollback: () => {
        if (sourceScheduleOverride === nextValue) sourceScheduleOverride = null;
      },
    };
  }

  function optimisticLibrarr(formData: FormData) {
    const nextUrl = String(formData.get("url") ?? "").trim();
    const nextMediaType = String(formData.get("mediaType")) as MediaType;
    librarrUrlOverride = nextUrl;
    librarrMediaTypeOverride = nextMediaType;
    librarrConnectedOverride = Boolean(String(formData.get("apiKey") ?? "").trim()) || librarrConnected;
    return {
      commit: () => {
        if (librarrUrlOverride === nextUrl) librarrUrlOverride = null;
        if (librarrMediaTypeOverride === nextMediaType) librarrMediaTypeOverride = null;
        librarrConnectedOverride = null;
      },
      rollback: () => {
        librarrUrlOverride = null;
        librarrConnectedOverride = null;
        librarrMediaTypeOverride = null;
      },
    };
  }

  function optimisticEmbeddings(formData: FormData) {
    const nextBackend = String(formData.get("backend") ?? "");
    const nextModel = String(formData.get("model") ?? "");
    const nextUrl = String(formData.get("url") ?? "");
    embeddingBackendOverride = nextBackend;
    embeddingModelOverride = nextModel;
    embeddingUrlOverride = nextUrl;
    return {
      commit: () => {
        embeddingBackendOverride = null;
        embeddingModelOverride = null;
        embeddingUrlOverride = null;
      },
      rollback: () => {
        embeddingBackendOverride = null;
        embeddingModelOverride = null;
        embeddingUrlOverride = null;
      },
    };
  }

  function optimisticRevokeToken(formData: FormData) {
    const id = Number(formData.get("id"));
    if (!Number.isInteger(id)) return;
    const previous = revokedApiTokenIds.has(id);
    revokedApiTokenIds = new Set([...revokedApiTokenIds, id]);
    return {
      commit: () => {
        const next = new Set(revokedApiTokenIds);
        next.delete(id);
        revokedApiTokenIds = next;
      },
      rollback: () => {
        if (previous) return;
        const next = new Set(revokedApiTokenIds);
        next.delete(id);
        revokedApiTokenIds = next;
      },
    };
  }

  function optimisticDigestSettings(formData: FormData) {
    const nextEnabled = formData.get("enabled") === "on";
    const nextOnlyNew = formData.get("onlyNew") === "on";
    const channels = formData.getAll("channels");
    const nextDiscord = channels.includes("discord");
    const nextEmail = channels.includes("email");
    digestEnabledOverride = nextEnabled;
    digestOnlyNewOverride = nextOnlyNew;
    digestDiscordOverride = nextDiscord;
    digestEmailOverride = nextEmail;
    const clear = () => {
      digestEnabledOverride = null;
      digestOnlyNewOverride = null;
      digestDiscordOverride = null;
      digestEmailOverride = null;
    };
    return {
      commit: clear,
      rollback: () => {
        clear();
      },
    };
  }
  function motionDuration(duration: number) {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    )
      return duration;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? 0
      : duration;
  }
  function motionDelay(index: number, step = 36) {
    return motionDuration(Math.min(index * step, 180));
  }
  function formatRelease(value: string | null, kind: string | null = null) {
    if (!value) return "";
    const date = new Date(`${value}T00:00:00`);
    if (Number.isNaN(date.valueOf())) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const label = date.toLocaleDateString(
      "en-US",
      kind === "year"
        ? { year: "numeric" }
        : kind === "month"
          ? { month: "short", year: "numeric" }
          : { month: "short", day: "numeric", year: "numeric" },
    );
    return `${date < today ? "Published" : "Publishes"} ${label}`;
  }

  function formatBackupTime(value: string | null) {
    if (!value) return "No successful backup yet";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "Unknown" : date.toLocaleString();
  }

  function formatBackupSize(bytes: number) {
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  async function fetchInitialFilteredDiscover(
    key: string,
    searchQuery: string,
    selectedGenre: string,
  ) {
    discoverFilterAbortController?.abort();
    const controller = new AbortController();
    discoverFilterAbortController = controller;
    discoverLoading = true;
    discoverLoadError = "";
    try {
      const params = new URLSearchParams({
        status: "recommended",
        limit: String(DISCOVER_PAGE_SIZE),
        offset: "0",
      });
      if (searchQuery) params.set("search", searchQuery);
      if (selectedGenre) params.set("genre", selectedGenre);
      const response = await fetch(`/api/recommendations?${params.toString()}`, {
        headers: profileHeaders,
        signal: controller.signal,
      });
      const payload = (await response.json().catch(() => ({}))) as {
        items?: PageBook[];
        has_more?: boolean;
        message?: string;
      };
      if (discoverFilterAbortController !== controller) return;
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Recommendations could not be loaded.");
      }
      filteredDiscoverBooks = payload.items;
      filteredDiscoverOffset = payload.items.length;
      filteredDiscoverHasMore = payload.has_more === true;
      filteredDiscoverLoadedKey = key;
      filteredDiscoverPending = false;
      discoverVisibleCount = DISCOVER_PAGE_SIZE;
    } catch (error) {
      if (controller.signal.aborted || discoverFilterAbortController !== controller) return;
      discoverLoadError =
        error instanceof Error
          ? error.message
          : "Recommendations could not be loaded.";
      filteredDiscoverBooks = [];
      filteredDiscoverOffset = 0;
      filteredDiscoverHasMore = false;
      filteredDiscoverLoadedKey = key;
      filteredDiscoverPending = false;
    } finally {
      if (discoverFilterAbortController === controller) {
        discoverFilterAbortController = null;
        discoverLoading = false;
      }
    }
  }

  function reloadFilteredDiscover() {
    if (!hasActiveDiscoverFilter) return;
    filteredDiscoverPending = true;
    void fetchInitialFilteredDiscover(
      activeDiscoverFilterKey,
      discoverSearch.trim(),
      discoverGenre.trim(),
    );
  }

  async function loadMoreDiscover() {
    if (discoverLoading || filteredDiscoverPending || digestRequested) return;
    if (discoverVisibleCount < discoverViewBooks.length) {
      discoverVisibleCount = Math.min(
        discoverVisibleCount + DISCOVER_PAGE_SIZE,
        discoverViewBooks.length,
      );
      return;
    }
    if (!effectiveDiscoverHasMore) return;

    const filteredMode = hasActiveDiscoverFilter;
    const expectedFilterKey = activeDiscoverFilterKey;
    const searchQuery = discoverSearch.trim();
    const selectedGenre = discoverGenre.trim();
    const offset = filteredMode ? filteredDiscoverOffset : unfilteredDiscoverOffset;
    const controller = new AbortController();
    discoverFilterAbortController = controller;
    discoverLoading = true;
    discoverLoadError = "";
    try {
      const params = new URLSearchParams({
        status: "recommended",
        limit: String(DISCOVER_PAGE_SIZE),
        offset: String(offset),
      });
      if (filteredMode && searchQuery) params.set("search", searchQuery);
      if (filteredMode && selectedGenre) params.set("genre", selectedGenre);
      const response = await fetch(`/api/recommendations?${params.toString()}`, {
        headers: profileHeaders,
        signal: controller.signal,
      });
      const payload = (await response.json().catch(() => ({}))) as {
        items?: PageBook[];
        has_more?: boolean;
        message?: string;
      };
      if (discoverFilterAbortController !== controller) return;
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Recommendations could not be loaded.");
      }
      if (filteredMode) {
        if (activeDiscoverFilterKey !== expectedFilterKey) return;
        const existing = new Set(filteredDiscoverBooks.map((book) => book.id));
        filteredDiscoverBooks = [
          ...filteredDiscoverBooks,
          ...payload.items.filter((book) => !existing.has(book.id)),
        ];
        filteredDiscoverOffset = offset + payload.items.length;
        filteredDiscoverHasMore = payload.has_more === true;
      } else {
        const existing = new Set(
          [...data.books, ...additionalDiscoverBooks].map((book) => book.id),
        );
        additionalDiscoverBooks = [
          ...additionalDiscoverBooks,
          ...payload.items.filter((book) => !existing.has(book.id)),
        ];
        unfilteredDiscoverOffset = offset + payload.items.length;
        unfilteredDiscoverHasMore = payload.has_more === true;
      }
      discoverVisibleCount += DISCOVER_PAGE_SIZE;
    } catch (error) {
      if (controller.signal.aborted || discoverFilterAbortController !== controller) return;
      discoverLoadError =
        error instanceof Error
          ? error.message
          : "Recommendations could not be loaded.";
    } finally {
      if (discoverFilterAbortController === controller) {
        discoverFilterAbortController = null;
        discoverLoading = false;
      }
    }
  }

  async function fetchInitialQueriedDecisions(
    key: string,
    apiStatus: string,
    searchQuery: string,
    selectedGenre: string,
  ) {
    decisionQueryAbortController?.abort();
    const controller = new AbortController();
    decisionQueryAbortController = controller;
    decisionsLoading = true;
    decisionsLoadError = "";
    try {
      const params = new URLSearchParams({
        status: apiStatus,
        limit: String(DECISION_PAGE_SIZE),
        offset: "0",
      });
      if (searchQuery) params.set("search", searchQuery);
      if (selectedGenre) params.set("genre", selectedGenre);
      const response = await fetch(`/api/recommendations?${params.toString()}`, {
        headers: profileHeaders,
        signal: controller.signal,
      });
      const payload = (await response.json().catch(() => ({}))) as {
        items?: PageBook[];
        has_more?: boolean;
        message?: string;
      };
      if (decisionQueryAbortController !== controller) return;
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Past decisions could not be loaded.");
      }
      queriedDecisionBooks = payload.items;
      queriedDecisionOffset = payload.items.length;
      queriedDecisionsHasMore = payload.has_more === true;
      queriedDecisionLoadedKey = key;
      queriedDecisionPending = false;
    } catch (error) {
      if (controller.signal.aborted || decisionQueryAbortController !== controller) return;
      decisionsLoadError =
        error instanceof Error
          ? error.message
          : "Past decisions could not be loaded.";
      queriedDecisionBooks = [];
      queriedDecisionOffset = 0;
      queriedDecisionsHasMore = false;
      queriedDecisionLoadedKey = key;
      queriedDecisionPending = false;
    } finally {
      if (decisionQueryAbortController === controller) {
        decisionQueryAbortController = null;
        decisionsLoading = false;
      }
    }
  }

  function reloadQueriedDecisions() {
    if (!isQueriedDecisionMode) return;
    queriedDecisionPending = true;
    void fetchInitialQueriedDecisions(
      activeDecisionQueryKey,
      activeDecisionApiStatus,
      decisionsSearch.trim(),
      decisionsGenre.trim(),
    );
  }

  async function loadMoreDecisions() {
    if (decisionsLoading || queriedDecisionPending || !decisionsHasMore) return;
    const queriedMode = isQueriedDecisionMode;
    const expectedKey = activeDecisionQueryKey;
    const apiStatus = activeDecisionApiStatus;
    const searchQuery = decisionsSearch.trim();
    const selectedGenre = decisionsGenre.trim();
    const offset = queriedMode ? queriedDecisionOffset : unfilteredDecisionOffset;
    const controller = new AbortController();
    decisionQueryAbortController = controller;
    decisionsLoading = true;
    decisionsLoadError = "";
    try {
      const params = new URLSearchParams({
        status: apiStatus,
        limit: String(DECISION_PAGE_SIZE),
        offset: String(offset),
      });
      if (queriedMode && searchQuery) params.set("search", searchQuery);
      if (queriedMode && selectedGenre) params.set("genre", selectedGenre);
      const response = await fetch(`/api/recommendations?${params.toString()}`, {
        headers: profileHeaders,
        signal: controller.signal,
      });
      const payload = (await response.json().catch(() => ({}))) as {
        items?: PageBook[];
        has_more?: boolean;
        message?: string;
      };
      if (decisionQueryAbortController !== controller) return;
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Past decisions could not be loaded.");
      }
      if (queriedMode) {
        if (activeDecisionQueryKey !== expectedKey) return;
        const existing = new Set(queriedDecisionBooks.map((book) => book.id));
        queriedDecisionBooks = [
          ...queriedDecisionBooks,
          ...payload.items.filter((book) => !existing.has(book.id)),
        ];
        queriedDecisionOffset = offset + payload.items.length;
        queriedDecisionsHasMore = payload.has_more === true;
      } else {
        const existing = new Set(
          [...data.decisions, ...additionalDecisionBooks].map((book) => book.id),
        );
        additionalDecisionBooks = [
          ...additionalDecisionBooks,
          ...payload.items.filter((book) => !existing.has(book.id)),
        ];
        unfilteredDecisionOffset = offset + payload.items.length;
        decisionsHasMoreOverride = payload.has_more === true;
      }
    } catch (error) {
      if (controller.signal.aborted || decisionQueryAbortController !== controller) return;
      decisionsLoadError = error instanceof Error
        ? error.message
        : "Past decisions could not be loaded.";
    } finally {
      if (decisionQueryAbortController === controller) {
        decisionQueryAbortController = null;
        decisionsLoading = false;
      }
    }
  }

  function toggleDigestBook(id: number) {
    const next = new Set(selectedDigestIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedDigestIds = next;
  }

  function selectVisibleDigestBooks(books: Array<{ id: number; status: string }>) {
    selectedDigestIds = new Set(
      books.filter((book) => book.status === "recommended").map((book) => book.id),
    );
  }

  function clearDigestSelection() {
    selectedDigestIds = new Set();
  }

  function exitDigestMode() {
    digestMode = false;
    clearDigestSelection();
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.delete("digest");
      url.searchParams.delete("digest_period");
      pushState(url, {});
    }
  }

  function formatSourceScan(value: string | null, status: string | null) {
    if (status?.startsWith("error:")) {
      const detail = status.slice("error:".length).trim().replace(/\s+/g, " ");
      if (!detail) return "Needs attention";
      const clipped = detail.length > 140 ? `${detail.slice(0, 139)}…` : detail;
      return `Needs attention: ${clipped}`;
    }
    if (!value) return "Not scanned yet";
    const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.valueOf())
      ? "Scanned"
      : `Scanned ${date.toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
  }

  function formatDeliveryDate(value: string | null | undefined) {
    if (!value) return "";
    const date = new Date(value.includes("T") ? value : `${value.replace(" ", "T")}Z`);
    return Number.isNaN(date.valueOf()) ? "" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function resultText(result: LibrarrResult, keys: string[], fallback: string) {
    for (const key of keys) {
      const value = result[key];
      if (typeof value === "string" && value.trim()) return value.trim();
      if (typeof value === "number") return String(value);
      if (Array.isArray(value)) {
        const values = value
          .filter(
            (item) => typeof item === "string" || typeof item === "number",
          )
          .map(String)
          .filter(Boolean);
        if (values.length) return values.join(", ");
      }
      if (value && typeof value === "object") {
        for (const nestedKey of ["url", "href", "name", "title"]) {
          const nested = (value as Record<string, unknown>)[nestedKey];
          if (typeof nested === "string" && nested.trim()) return nested.trim();
        }
      }
    }
    return fallback;
  }
  function resultCover(result: LibrarrResult) {
    const value = resultText(
      result,
      ["cover_url", "coverUrl", "cover", "image", "thumbnail", "poster"],
      "",
    );
    try {
      const parsed = new URL(value);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
    } catch {
      return "";
    }
  }
  function resultTitle(result: LibrarrResult) {
    return resultText(result, ["title", "name"], "Untitled result");
  }
  function resultAuthor(result: LibrarrResult) {
    return resultText(
      result,
      ["author", "authors", "artist"],
      "Author unavailable",
    );
  }
  function resultFormat(result: LibrarrResult) {
    return resolveLibrarrResultFormat(result, librarrSearchedMediaType);
  }
  function resultScore(result: LibrarrResult) {
    const value = result.score;
    return typeof value === "number" && Number.isFinite(value)
      ? Math.max(0, Math.min(100, value))
      : null;
  }
  const highConfidenceLibrarrResults = $derived(
    librarrResults
      .map((result, index) => ({ result, index, score: resultScore(result) }))
      .filter((item): item is { result: LibrarrResult; index: number; score: number } => item.score !== null && item.score >= 90),
  );
  function resultKey(result: LibrarrResult, index: number) {
    return `${resultIdentity(result)}-${index}`;
  }
  function resultIdentity(result: LibrarrResult) {
    return resultText(
      result,
      ["id", "guid", "isbn", "asin"],
      `${resultTitle(result)}-${resultAuthor(result)}-${resultFormat(result)}`,
    ).trim().toLocaleLowerCase();
  }
  function openLibrarrSearch(book: {
    id: number;
    title: string;
    author: string;
  }) {
    if (!librarrConnected) return;
    librarrSearchBook = book;
    librarrQuery = `${book.title} ${book.author}`.trim().slice(0, 200);
    librarrMediaType = configuredLibrarrMediaType;
    librarrSearchedMediaType = configuredLibrarrMediaType;
    librarrResults = [];
    librarrSearchState = "idle";
    librarrSearchError = "";
    librarrSearchMessage = "";
    librarrAdded = new Set();
    librarrSearchOpen = true;
    void searchLibrarr();
  }
  function openMarkRead(book: PageBook) {
    openReadMenuId = null;
    markReadBook = book;
    markReadRating = 0;
  }
  function closeMarkRead() {
    markReadBook = null;
    markReadRating = 0;
  }
  function closeLibrarrSearch() {
    librarrSearchAbortController?.abort();
    librarrSearchAbortController = null;
    librarrSearchOpen = false;
  }

  function recordBookEvent(
    candidateId: number,
    eventType: "visible" | "detail_open" | "source_open" | "librarr_search" | "librarr_import",
    metadata: Record<string, unknown> = {},
  ) {
    const runId = String(allBooks.find((book) => book.id === candidateId)?.recommendation_run_id || "");
    telemetry.enqueue({
      candidate_id: candidateId,
      event_type: eventType,
      ...(runId.length >= 8 ? { run_id: runId } : {}),
      metadata,
    });
  }

  function trackRecommendation(
    node: HTMLElement,
    detail: { candidateId: number },
  ) {
    let sent = false;
    const markVisible = () => {
      if (sent) return;
      sent = true;
      recordBookEvent(detail.candidateId, "visible");
      observer?.disconnect();
    };
    const observer =
      typeof IntersectionObserver === "undefined"
        ? undefined
        : new IntersectionObserver(
            (entries) => {
              if (entries.some((entry) => entry.isIntersecting)) markVisible();
            },
            { threshold: 0.25 },
          );
    if (observer) observer.observe(node);
    else markVisible();
    return {
      destroy() {
        observer?.disconnect();
      },
    };
  }

  async function copyApiToken() {
    apiTokenCopyMessage = await copyApiTokenText(
      revealedApiToken ?? "",
      navigator.clipboard,
    );
  }

  $effect(() => {
    if (formState?.token) revealedApiToken = formState.token;
    else if (formState) revealedApiToken = null;
  });

  $effect(() => {
    const nextProfileId = data.user?.profile_id;
    if (previousProfileId === nextProfileId) return;
    previousProfileId = nextProfileId;
    resetClientPagination();
    resetReadingHistory();
  });

  $effect(() => {
    const currentUrl = page.shallow?.url ?? page.url;
    syncFromUrl(currentUrl);
  });

  $effect(() => {
    if (
      activeView === "decisions" &&
      decisionFilter === "read" &&
      readingHistoryState === "idle"
    ) {
      void loadReadingHistory();
    }
  });

  $effect(() => {
    const nextBooks = data.books;
    const nextRecommendationsHasMore = Boolean(data.recommendations_has_more);
    const nextDecisions = data.decisions;
    const nextDecisionsHasMore = Boolean(data.decisions_has_more);
    if (
      previousDataBooks === nextBooks &&
      previousDataRecommendationsHasMore === nextRecommendationsHasMore &&
      previousDataDecisions === nextDecisions &&
      previousDataDecisionsHasMore === nextDecisionsHasMore
    ) {
      return;
    }
    const prevDiscover = untrack(() => additionalDiscoverBooks);
    const prevDecisions = untrack(() => additionalDecisionBooks);
    const prevDiscoverHasMore = untrack(() => unfilteredDiscoverHasMore);
    const prevDecisionsOverride = untrack(() => decisionsHasMoreOverride);
    previousDataBooks = nextBooks;
    previousDataRecommendationsHasMore = nextRecommendationsHasMore;
    previousDataDecisions = nextDecisions;
    previousDataDecisionsHasMore = nextDecisionsHasMore;
    const reconciled = reconcilePagedBooks(
      nextBooks,
      nextDecisions,
      prevDiscover,
      prevDecisions,
    );
    additionalDiscoverBooks = reconciled.additionalDiscoverBooks;
    additionalDecisionBooks = reconciled.additionalDecisionBooks;
    unfilteredDiscoverOffset =
      nextBooks.filter((book) => book.status === "recommended").length +
      reconciled.additionalDiscoverBooks.length;
    unfilteredDiscoverHasMore =
      nextRecommendationsHasMore &&
      (prevDiscover.length > 0 ? prevDiscoverHasMore : true);
    unfilteredDecisionOffset =
      nextDecisions.length + reconciled.additionalDecisionBooks.length;
    decisionsHasMoreOverride = !nextDecisionsHasMore
      ? false
      : prevDecisions.length > 0 && prevDecisionsOverride === false
        ? false
        : null;
    discoverLoadError = "";
    decisionsLoadError = "";
  });

  $effect(() => {
    const nextSearch = discoverSearch;
    const nextGenre = discoverGenre;
    if (
      previousDiscoverSearch === nextSearch &&
      previousDiscoverGenre === nextGenre
    ) {
      return;
    }
    previousDiscoverSearch = nextSearch;
    previousDiscoverGenre = nextGenre;
    discoverVisibleCount = DISCOVER_PAGE_SIZE;
  });

  $effect(() => {
    if (discoverFilterDebounceTimer) {
      clearTimeout(discoverFilterDebounceTimer);
      discoverFilterDebounceTimer = null;
    }
    const filtered = hasActiveDiscoverFilter;
    const key = activeDiscoverFilterKey;
    const loadedKey = untrack(() => filteredDiscoverLoadedKey);
    const searchQuery = discoverSearch.trim();
    const selectedGenre = discoverGenre.trim();
    if (!filtered) {
      discoverFilterAbortController?.abort();
      discoverFilterAbortController = null;
      filteredDiscoverPending = false;
      filteredDiscoverBooks = [];
      filteredDiscoverOffset = 0;
      filteredDiscoverHasMore = false;
      filteredDiscoverLoadedKey = "";
      discoverLoading = false;
      discoverLoadError = "";
      return;
    }
    if (loadedKey === key) return;
    filteredDiscoverPending = true;
    discoverLoadError = "";
    discoverFilterDebounceTimer = setTimeout(() => {
      discoverFilterDebounceTimer = null;
      void fetchInitialFilteredDiscover(key, searchQuery, selectedGenre);
    }, FILTER_DEBOUNCE_MS);
    return () => {
      if (discoverFilterDebounceTimer) {
        clearTimeout(discoverFilterDebounceTimer);
        discoverFilterDebounceTimer = null;
      }
    };
  });

  $effect(() => {
    if (decisionQueryDebounceTimer) {
      clearTimeout(decisionQueryDebounceTimer);
      decisionQueryDebounceTimer = null;
    }
    const queried = isQueriedDecisionMode;
    const key = activeDecisionQueryKey;
    const apiStatus = activeDecisionApiStatus;
    const loadedKey = untrack(() => queriedDecisionLoadedKey);
    const searchQuery = decisionsSearch.trim();
    const selectedGenre = decisionsGenre.trim();
    if (!queried) {
      decisionQueryAbortController?.abort();
      decisionQueryAbortController = null;
      queriedDecisionPending = false;
      queriedDecisionBooks = [];
      queriedDecisionOffset = 0;
      queriedDecisionsHasMore = false;
      queriedDecisionLoadedKey = "";
      previousQueriedDecisionStatus = apiStatus;
      decisionsLoading = false;
      decisionsLoadError = "";
      return;
    }
    const statusChanged = previousQueriedDecisionStatus !== apiStatus;
    previousQueriedDecisionStatus = apiStatus;
    if (loadedKey === key) return;
    queriedDecisionPending = true;
    decisionsLoadError = "";
    const delay = statusChanged || loadedKey === "" && !searchQuery && !selectedGenre
      ? 0
      : FILTER_DEBOUNCE_MS;
    if (delay === 0) {
      void fetchInitialQueriedDecisions(key, apiStatus, searchQuery, selectedGenre);
      return;
    }
    decisionQueryDebounceTimer = setTimeout(() => {
      decisionQueryDebounceTimer = null;
      void fetchInitialQueriedDecisions(key, apiStatus, searchQuery, selectedGenre);
    }, delay);
    return () => {
      if (decisionQueryDebounceTimer) {
        clearTimeout(decisionQueryDebounceTimer);
        decisionQueryDebounceTimer = null;
      }
    };
  });

  $effect(() => {
    const selected = discoverGenre.trim().toLowerCase();
    if (selected && !discoverGenres.some((genre) => genre.toLowerCase() === selected)) {
      discoverGenre = "";
    }
  });

  $effect(() => {
    const selected = savedGenre.trim().toLowerCase();
    if (selected && !savedGenres.some((genre) => genre.toLowerCase() === selected)) {
      savedGenre = "";
    }
  });

  $effect(() => {
    if (decisionFilter === "read") return;
    const selected = decisionsGenre.trim().toLowerCase();
    if (selected && !decisionGenres.some((genre) => genre.toLowerCase() === selected)) {
      decisionsGenre = "";
    }
  });

  $effect(() => {
    const sentinel = loadMoreSentinel;
    const canLoadMore = hasMoreDiscoverBooks;
    const loading = discoverLoading;
    const pending = filteredDiscoverPending;
    const hasError = Boolean(discoverLoadError);
    const visibleCount = discoverViewVisibleBooks.length;
    const serverOffset = hasActiveDiscoverFilter
      ? filteredDiscoverOffset
      : unfilteredDiscoverOffset;
    void visibleCount;
    void serverOffset;
    if (
      !sentinel ||
      !canLoadMore ||
      loading ||
      pending ||
      hasError ||
      typeof IntersectionObserver === "undefined"
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMoreDiscover();
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  });
  function handleLibrarrMediaTypeChange(event: Event) {
    const nextMediaType: MediaType =
      (event.currentTarget as HTMLSelectElement).value === "ebook"
        ? "ebook"
        : "audiobook";
    librarrMediaType = nextMediaType;
    librarrSearchAbortController?.abort();
    librarrSearchAbortController = null;
    const action = librarrFormatChangeAction(librarrQuery, nextMediaType);
    if (action.shouldSearch) {
      void searchLibrarr();
      return;
    }
    librarrSearchedMediaType = action.searchedMediaType;
    librarrResults = [];
    librarrAdded = new Set();
    librarrSearchState = action.state;
    librarrSearchError = "";
    librarrSearchMessage = "";
  }
  async function searchLibrarr() {
    const query = librarrQuery.trim();
    librarrSearchAbortController?.abort();
    librarrSearchAbortController = null;
    if (query.length < 2) {
      librarrSearchState = "error";
      librarrSearchError = "Enter at least two characters.";
      return;
    }
    const searchedMediaType = librarrMediaType;
    librarrSearchedMediaType = searchedMediaType;
    const controller = new AbortController();
    librarrSearchAbortController = controller;
    librarrResults = [];
    librarrAdded = new Set();
    librarrSearchState = "searching";
    librarrSearchError = "";
    librarrSearchMessage = "";
    try {
      const params = new URLSearchParams({
        q: query,
        media_type: searchedMediaType,
      });
      if (librarrSearchBook) recordBookEvent(librarrSearchBook.id, "librarr_search", { query });
      let response = await fetch(`/api/librarr/search/stream?${params.toString()}`, {
        headers: { accept: "text/event-stream", ...profileHeaders },
        signal: controller.signal,
      });
      if (librarrSearchAbortController !== controller) return;
      const isEventStream = response.headers
        .get("content-type")
        ?.toLowerCase()
        .startsWith("text/event-stream");
      const streamUnsupported = [404, 405, 501].includes(response.status) ||
        (response.ok && !isEventStream);
      if (streamUnsupported) {
        await response.body?.cancel().catch(() => {});
        response = await fetch(`/api/librarr/search?${params.toString()}`, {
          headers: profileHeaders,
          signal: controller.signal,
        });
        if (librarrSearchAbortController !== controller) return;
        const payload = (await response.json().catch(() => ({}))) as {
          results?: unknown[];
          message?: string;
        };
        if (!response.ok)
          throw new Error(payload.message || "Librarr search failed.");
        librarrResults = Array.isArray(payload.results)
          ? payload.results.filter((item): item is LibrarrResult =>
              Boolean(item && typeof item === "object" && !Array.isArray(item)),
            )
          : [];
        if (librarrSearchAbortController === controller) {
          librarrSearchState = "ready";
        }
      } else {
        if (!response.ok) {
          const payload = (await response.json().catch(() => ({}))) as {
            message?: string;
          };
          throw new Error(payload.message || "Librarr search failed.");
        }
        if (!isEventStream) throw new Error("Librarr did not return a search stream.");
        const progress = await readLibrarrSearchStream(response, (results) => {
          if (librarrSearchAbortController === controller) librarrResults = results;
        });
        if (librarrSearchAbortController !== controller) return;
        const outcome = finalizeLibrarrStreamState(
          progress.completed,
          librarrResults.length,
        );
        librarrSearchState = outcome.state;
        librarrSearchError = outcome.error;
      }
    } catch (error) {
      if (controller.signal.aborted || librarrSearchAbortController !== controller) return;
      librarrSearchState = "error";
      librarrSearchError =
        error instanceof Error ? error.message : "Librarr search failed.";
    } finally {
      if (librarrSearchAbortController === controller) {
        librarrSearchAbortController = null;
      }
    }
  }
  async function addLibrarrResult(result: LibrarrResult, index: number, closeAfter = true) {
    const identity = resultIdentity(result);
    if (librarrAdded.has(identity)) return;
    librarrAddingKey = identity;
    librarrAdded = new Set([...librarrAdded, identity]);
    librarrSearchError = "";
    librarrSearchMessage = "";
    try {
      const runId = librarrSearchBook
        ? String(allBooks.find((book) => book.id === librarrSearchBook?.id)?.recommendation_run_id || "").trim()
        : "";
      const response = await fetch("/api/librarr/download", {
        method: "POST",
        headers: { "content-type": "application/json", ...profileHeaders },
        body: JSON.stringify(
          buildLibrarrDownloadPayload(
            librarrSearchedMediaType,
            result,
            librarrSearchBook?.id,
            runId,
          ),
        ),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        message?: string;
      };
      if (!response.ok)
        throw new Error(payload.message || "Librarr could not add that book.");
      librarrSearchMessage = `${resultTitle(result)} added to Librarr.`;
      if (closeAfter) {
        announceActionSuccess(`${resultTitle(result)} added to Librarr.`);
        closeLibrarrSearch();
        await invalidateAll();
      }
      return true;
    } catch (error) {
      const nextAdded = new Set(librarrAdded);
      nextAdded.delete(identity);
      librarrAdded = nextAdded;
      librarrSearchError =
        error instanceof Error
          ? error.message
          : "Librarr could not add that book.";
      return false;
    } finally {
      if (librarrAddingKey === identity) librarrAddingKey = null;
    }
  }
  async function addHighConfidenceLibrarrResults() {
    if (!highConfidenceLibrarrResults.length || librarrAddingKey !== null || librarrBulkAdding) return;
    librarrBulkAdding = true;
    librarrSearchError = "";
    librarrSearchMessage = "";
    let added = 0;
    let failed = 0;
    for (const item of highConfidenceLibrarrResults) {
      const success = await addLibrarrResult(item.result, item.index, false);
      if (success) added += 1;
      else failed += 1;
    }
    librarrBulkAdding = false;
    if (failed) {
      librarrSearchMessage = added ? `${added} high-confidence match${added === 1 ? "" : "es"} added.` : "No high-confidence matches were added.";
      librarrSearchError = `${failed} match${failed === 1 ? "" : "es"} could not be added. Review the remaining results and try again.`;
      if (added) await invalidateAll();
    } else if (added) {
      announceActionSuccess(`${added} high-confidence match${added === 1 ? "" : "es"} added to Librarr.`);
      closeLibrarrSearch();
      await invalidateAll();
    }
  }

  onMount(() => {
    const onPopState = () => {
      syncFromUrl(new URL(window.location.href));
    };
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && markReadBook) closeMarkRead();
      if (event.key === "Escape" && librarrSearchOpen) closeLibrarrSearch();
      if (event.key === "Escape" && openReadMenuId !== null) {
        const menuId = openReadMenuId;
        openReadMenuId = null;
        document.querySelector<HTMLButtonElement>(`[data-read-menu-trigger="${menuId}"]`)?.focus();
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (openReadMenuId !== null && target instanceof Node) {
        const menuRoot = document.querySelector<HTMLElement>(`[data-read-menu-root="${openReadMenuId}"]`);
        if (menuRoot && !menuRoot.contains(target)) openReadMenuId = null;
      }
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("keydown", onKeydown);
    document.addEventListener("pointerdown", onPointerDown);
    const context = (
      document as Document & {
        modelContext?: {
          registerTool: (
            tool: unknown,
            options?: { signal?: AbortSignal },
          ) => void | Promise<void>;
        };
      }
    ).modelContext;
    const lifecycle = new AbortController();
    if (context?.registerTool) {
      void Promise.resolve(
        context.registerTool(
          {
            name: "shortlist_book",
            title: "Shortlist a recommendation",
            description:
              "Save one currently recommended book to the Bookward shortlist using its numeric recommendation ID.",
            inputSchema: {
              type: "object",
              properties: { id: { type: "integer", minimum: 1 } },
              required: ["id"],
              additionalProperties: false,
            },
            annotations: { readOnlyHint: false, untrustedContentHint: false },
            async execute(input: unknown) {
              const id = Number((input as { id?: unknown })?.id);
              if (
                !Number.isInteger(id) ||
                !allBooks.some(
                  (book) => book.id === id && book.status === "recommended",
                )
              )
                throw new Error("Choose a current recommendation ID.");
              const body = new URLSearchParams({
                id: String(id),
                status: "saved",
                run_id: String(allBooks.find((book) => book.id === id)?.recommendation_run_id || ""),
              });
              const change = optimisticBookStatus(id, "saved");
              try {
                const response = await fetch("?/decide", {
                  method: "POST",
                  headers: {
                    accept: "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "x-sveltekit-action": "true",
                  },
                  body,
                });
                if (!response.ok) throw new Error("Could not shortlist that book.");
                await applyAction(deserialize(await response.text()));
                await invalidateAll();
                change?.commit?.();
                go("saved");
                return { id, status: "saved" };
              } catch (error) {
                change?.rollback?.();
                announceActionError(
                  error instanceof Error ? error.message : "Could not shortlist that book.",
                );
                throw error;
              }
            },
          },
          { signal: lifecycle.signal },
        ),
      ).catch(() => undefined);
    }
    return () => {
      lifecycle.abort();
      if (discoverFilterDebounceTimer) clearTimeout(discoverFilterDebounceTimer);
      if (decisionQueryDebounceTimer) clearTimeout(decisionQueryDebounceTimer);
      discoverFilterAbortController?.abort();
      decisionQueryAbortController?.abort();
      void telemetry.flush({ beacon: true });
      telemetry.destroy();
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("keydown", onKeydown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  });
</script>

<svelte:head>
  <title>{activeView === "saved" ? "Shortlist · Bookward" : activeView === "decisions" && decisionFilter === "read" ? "Reading history · Bookward" : activeView === "decisions" ? "History · Bookward" : activeView === "sources" ? "Sources · Bookward" : activeView === "settings" ? "Settings · Bookward" : "Discover · Bookward"}</title>
  <meta
    name="description"
    content="Personal book recommendations from your Goodreads history and trusted upcoming-book lists."
  />
</svelte:head>

<a
  href="#main-content"
  class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500"
  >Skip to content</a
>

<div
  data-sticky-shell
  class="relative isolate min-h-screen overflow-x-clip text-surface-900-100 before:pointer-events-none before:fixed before:inset-0 before:-z-10 before:content-[''] before:bg-[radial-gradient(circle_at_14%_2%,_color-mix(in_oklab,_var(--color-primary-500)_72%,_transparent)_0%,_transparent_36%),radial-gradient(circle_at_86%_8%,_color-mix(in_oklab,_var(--color-secondary-500)_60%,_transparent)_0%,_transparent_32%),radial-gradient(circle_at_52%_100%,_color-mix(in_oklab,_var(--color-tertiary-500)_64%,_transparent)_0%,_transparent_44%)] before:opacity-70 after:pointer-events-none after:fixed after:inset-0 after:-z-10 after:content-[''] after:bg-[linear-gradient(118deg,_transparent_0%,_color-mix(in_oklab,_var(--color-primary-500)_22%,_transparent)_42%,_transparent_68%),linear-gradient(180deg,_transparent_50%,_color-mix(in_oklab,_var(--color-secondary-500)_18%,_transparent)_100%)] after:opacity-90 dark:before:opacity-90 dark:after:opacity-100"
>
  <header
    class="sticky top-0 z-40 border-b border-primary-500/15 preset-filled-surface-50-950 shadow-lg shadow-primary-500/5 backdrop-blur-xl"
  >
    <div
      class="relative mx-auto flex min-h-16 max-w-7xl items-center gap-3 px-4 sm:px-6 lg:gap-6 lg:px-8"
    >
      <a
        href="/"
        class="flex min-h-11 shrink-0 items-center gap-3 text-surface-950-50 no-underline"
        aria-label="Bookward home"
        onclick={onHomeClick}
        ><span class="grid size-10 overflow-hidden rounded-2xl shadow-lg shadow-primary-500/20"
          ><img src={bookwardMark} alt="" width="128" height="128" class="size-full object-cover" aria-hidden="true" /></span
        ><span class="hidden text-lg font-semibold tracking-tight sm:inline md:hidden lg:inline"
          >Bookward</span
        ></a
      >
      <nav
        class="hidden min-w-0 flex-1 items-center gap-1 md:flex"
        aria-label="Primary navigation"
      >
        {#each navItems as item}{@const Icon = item.icon}<button
            type="button"
            class={`btn btn-sm min-h-11 ${activeView === item.id ? "preset-filled-primary-500" : "preset-tonal-surface text-surface-700-300"}`}
            aria-current={activeView === item.id ? "page" : undefined}
            onclick={() => go(item.id)}
            ><Icon size={16} strokeWidth={1.8} /><span>{item.label}</span>{#if item.id === "saved"}<span class="badge badge-sm preset-tonal-secondary">{shortlistTotalCount}</span>{/if}</button
      >{/each}
      </nav>
      <div class="ml-auto flex items-center gap-2">
        {#if activeView === "discover" || activeView === "saved"}<form
            method="POST"
            action="?/runSync"
            use:enhance={setPending("sync")}
            in:fade={{ duration: motionDuration(180) }}
            out:fade={{ duration: motionDuration(120) }}
          >
          <ProfileField profileId={data.user?.profile_id} />
            <button
              class="btn btn-icon min-h-11 min-w-11 preset-tonal-primary"
              type="submit"
              aria-label="Refresh recommendations"
              aria-busy={isPending("sync")}
              ><RefreshCw
                size={17}
                class={isPending("sync") ? "animate-spin" : ""}
              /></button
            >
          </form>{/if}
        <PopoverDialog
          bind:open={profileMenuOpen}
          panelId="profile-menu-panel"
          triggerLabel="Open profile menu"
          triggerTitle="Profile"
          dialogLabel="Profile"
          panelClass="absolute right-0 top-[calc(100%+0.5rem)] z-50 max-h-[calc(100dvh-5rem)] w-[min(20rem,calc(100vw-2rem))] overflow-y-auto rounded-container border border-surface-200-800 preset-filled-surface-50-950 p-3 shadow-xl"
        >
          {#snippet trigger()}
            <CircleUserRound size={19} strokeWidth={1.8} aria-hidden="true" />
          {/snippet}
          {#snippet children()}
            <div class="border-b border-surface-300-700/40 px-2 pb-3">
              <p class="truncate text-sm font-semibold text-surface-950-50">{data.user?.display_name || data.user?.username || "Account"}</p>
              {#if data.user?.role}<p class="mt-0.5 text-xs capitalize text-surface-700-300">{data.user.role}</p>{/if}
            </div>
            <div class="grid gap-1 py-2">
              <a href="/account" class="flex min-h-11 items-center gap-3 rounded-container px-3 text-sm text-surface-800-200 no-underline hover:preset-tonal-surface"><CircleUserRound size={17} aria-hidden="true" />Account settings</a>
              {#if data.user?.role === "admin"}<a href="/admin" class="flex min-h-11 items-center gap-3 rounded-container px-3 text-sm text-surface-800-200 no-underline hover:preset-tonal-surface"><KeyRound size={17} aria-hidden="true" />Manage accounts</a>{/if}
              <ThemePicker compact />
            </div>
            <form method="POST" action="/auth/logout" class="border-t border-surface-300-700/40 pt-2">
              <ProfileField profileId={data.user?.profile_id} />
              <button class="flex min-h-11 w-full items-center gap-3 rounded-container px-3 text-left text-sm text-surface-800-200 hover:preset-tonal-surface" type="submit"><LogOut size={17} aria-hidden="true" />Sign out</button>
            </form>
          {/snippet}
        </PopoverDialog>
      </div>
    </div>
  </header>

  <div
    class="mx-auto flex max-w-7xl gap-10 px-4 pb-28 pt-8 sm:px-6 lg:px-8 lg:pb-14 lg:pt-12"
  >
    <main id="main-content" tabindex="-1" class="min-w-0 flex-1">
      <div
        class="sr-only"
        role={formIsError ? "alert" : "status"}
        aria-live={formIsError ? "assertive" : "polite"}
        aria-atomic="true"
      >{visibleNotification?.message ?? ""}</div>
      {#if visibleNotification}
        {@const notificationUndoDecisions = visibleNotification.undo_decisions?.length
          ? visibleNotification.undo_decisions
          : visibleNotification.undo_id && visibleNotification.id
            ? [{ id: visibleNotification.id, decision_id: visibleNotification.undo_id }]
            : []}
        {@const notificationUndoKey = notificationUndoDecisions.length
          ? `undo-${notificationUndoDecisions.map((item) => item.decision_id).join("-")}`
          : null}
        {#key notificationUndoKey ?? visibleNotification.id ?? visibleNotification.message}
          <div
            class="pointer-events-none fixed inset-x-0 top-[calc(env(safe-area-inset-top)+4.75rem)] z-50 flex justify-end px-4 sm:inset-x-auto sm:right-5 sm:w-[min(26rem,calc(100vw-2.5rem))] sm:px-0"
          >
            <div
              in:fly={{ x: 24, y: -8, duration: motionDuration(240) }}
              out:fly={{ x: 16, duration: motionDuration(170) }}
              class={`pointer-events-auto relative w-full max-w-md overflow-hidden rounded-2xl border bg-surface-50-950/90 p-4 text-sm shadow-2xl shadow-primary-500/10 backdrop-blur-xl ${formIsError ? "border-error-500/25" : "border-success-500/25"}`}
              role="group"
              aria-label={formIsError ? "Action error notification" : "Action success notification"}
              onpointerenter={() => {
                notificationPointerInside = true;
                updateNotificationPause();
              }}
              onpointerleave={() => {
                notificationPointerInside = false;
                updateNotificationPause();
              }}
              onfocusin={() => {
                notificationFocusInside = true;
                updateNotificationPause();
              }}
              onfocusout={onNotificationFocusOut}
            >
              <span
                aria-hidden="true"
                class={`absolute inset-y-0 left-0 w-1 ${formIsError ? "bg-error-500" : "bg-success-500"}`}
              ></span>
              <div class="flex items-start gap-3 pl-1">
                <span
                  class={`mt-0.5 grid size-9 shrink-0 place-items-center rounded-xl ${formIsError ? "preset-tonal-error" : "preset-tonal-success"}`}
                  aria-hidden="true"
                >
                  {#if formIsError}<CircleHelp size={18} />{:else}<Check size={18} />{/if}
                </span>
                <div class="min-w-0 flex-1">
                  <p
                    class={`mb-1 text-[0.65rem] font-semibold uppercase tracking-[0.16em] ${formIsError ? "text-error-600-400" : "text-success-600-400"}`}
                  >
                    {formIsError ? "Action needs attention" : "All set"}
                  </p>
                  <p class="leading-relaxed text-surface-900-100">
                    {visibleNotification.message}
                  </p>
                  {#if notificationUndoDecisions.length && notificationUndoKey}
                    <form
                      method="POST"
                      action="?/undoDecision"
                      class="mt-3"
                      use:enhance={setPending(notificationUndoKey, optimisticUndoDecision)}
                    >
                      <ProfileField profileId={data.user?.profile_id} />
                      {#each notificationUndoDecisions as item (`${item.id}-${item.decision_id}`)}
                        <input type="hidden" name="id" value={item.id} />
                        <input type="hidden" name="decision_id" value={item.decision_id} />
                      {/each}
                      <button
                        type="submit"
                        class="btn btn-sm min-h-9 preset-tonal-primary"
                        disabled={isPending(notificationUndoKey)}
                        aria-busy={isPending(notificationUndoKey)}
                        aria-label={`Undo ${visibleNotification.undo_label ?? "this decision"}`}
                      >
                        {#if isPending(notificationUndoKey)}<RefreshCw size={14} class="animate-spin" />{:else}<History size={14} />{/if}
                        Undo
                      </button>
                    </form>
                  {/if}
                </div>
                <button
                  type="button"
                  class="btn btn-icon btn-sm min-h-8 min-w-8 shrink-0 preset-tonal-surface"
                  aria-label="Dismiss notification"
                  onclick={dismissNotification}
                >
                  <X size={15} />
                </button>
              </div>
            </div>
          </div>
        {/key}
      {/if}

      {#snippet recommendationView(view: "discover" | "saved" | "decisions")}

        {@const viewSearch = searchForView(view)}
        {@const viewGenre = genreForView(view)}
        {@const viewGenres = genresForView(view)}
        {@const viewHasFilter = Boolean(viewSearch.trim() || viewGenre.trim())}
        {@const viewBooks = view === "discover" ? discoverViewBooks : view === "saved" ? shortlistBooks : decisionBooks}
        {@const viewVisibleBooks = view === "discover" ? discoverViewVisibleBooks : viewBooks}
        {@const viewHasMoreDiscoverBooks = view === "discover" && (viewVisibleBooks.length < viewBooks.length || effectiveDiscoverHasMore)}
        {@const viewInitialLoading = (view === "discover" && hasActiveDiscoverFilter && (filteredDiscoverPending || (discoverLoading && filteredDiscoverLoadedKey !== activeDiscoverFilterKey))) || (view === "decisions" && isQueriedDecisionMode && (queriedDecisionPending || (decisionsLoading && queriedDecisionLoadedKey !== activeDecisionQueryKey)))}
        {@const viewFilterError = view === "discover" && hasActiveDiscoverFilter ? discoverLoadError : view === "decisions" && isQueriedDecisionMode ? decisionsLoadError : ""}
        <section class="mb-8 flex flex-col gap-5 px-1 sm:px-0 xl:flex-row xl:items-end xl:justify-between">
          <h1
            class="text-4xl font-semibold leading-[1.05] tracking-tight text-surface-950-50 sm:text-6xl"
          >{view === "discover" ? "Find your next favorite." : view === "saved" ? "Your shortlist." : view === "decisions" && decisionFilter === "read" ? "Your reading history." : "Past decisions."}</h1>
          <div class="flex w-full flex-col gap-3 xl:max-w-2xl sm:flex-row sm:items-center">
              <div class="input flex min-h-11 w-full min-w-0 items-center gap-1 px-0! py-0! sm:flex-1">
                <label class="flex min-h-11 min-w-0 flex-1 cursor-text items-center gap-2 self-stretch pl-3 pr-2">
                  <Search size={17} class="shrink-0 text-surface-600-400" />
                  <input
                    value={viewSearch}
                    oninput={(event) => setSearchForView(view, event.currentTarget.value)}
                    class="input-ghost min-h-11 min-w-0 flex-1 self-stretch"
                    aria-label={view === "saved" ? "Search shortlist" : view === "decisions" && decisionFilter === "read" ? "Search reading history" : view === "decisions" ? "Search past decisions" : "Search recommendations"}
                    placeholder={view === "saved" ? "Search shortlist" : "Search books"}
                  />
                </label>
                {#if viewSearch}<button type="button" class="btn-icon btn-icon-sm mr-1.5 min-h-8 min-w-8 shrink-0 preset-tonal-surface" aria-label="Clear book search" onclick={() => setSearchForView(view, "")}><X size={15} /></button>{/if}
              </div>
              {#if viewGenres.length && !(view === "decisions" && decisionFilter === "read")}
                <label class="block min-w-0 text-xs font-medium text-surface-700-300 sm:w-44">
                  Genre
                  <select
                    class="select mt-1 min-h-11 w-full preset-tonal-surface"
                    value={viewGenre}
                    onchange={(event) => setGenreForView(view, event.currentTarget.value)}
                    aria-label={view === "saved" ? "Filter shortlist by genre" : view === "decisions" ? "Filter past decisions by genre" : "Filter books by genre"}
                  >
                    <option value="">All genres</option>
                    {#each viewGenres as genre (genre)}<option value={genre} selected={viewGenre.toLowerCase() === genre.toLowerCase()}>{genre}</option>{/each}
                  </select>
                </label>
              {/if}
              {#if view === "saved"}
                <label class="block min-w-0 text-xs font-medium text-surface-700-300 sm:w-44">
                  Sort by
                  <select class="select mt-1 min-h-11 w-full preset-tonal-surface" bind:value={shortlistSort} aria-label="Sort shortlist">
                    <option value="score">Best match</option>
                    <option value="title">Title A–Z</option>
                    <option value="author">Author A–Z</option>
                  </select>
                </label>
              {/if}
          </div>
        </section>
        {#if view === "decisions"}
          <section class="mb-6" aria-label="History filters">
            <div class="flex flex-wrap gap-2" role="group" aria-label="Filter history">
              <button type="button" class={`btn btn-sm min-h-10 ${decisionFilter === "all" ? "preset-filled-primary-500" : "preset-tonal-surface"}`} aria-pressed={decisionFilter === "all"} onclick={() => selectDecisionFilter("all")}>All</button>
              <button type="button" class={`btn btn-sm min-h-10 ${decisionFilter === "maybe_later" ? "preset-filled-primary-500" : "preset-tonal-surface"}`} aria-pressed={decisionFilter === "maybe_later"} onclick={() => selectDecisionFilter("maybe_later")}>Maybe later</button>
              <button type="button" class={`btn btn-sm min-h-10 ${decisionFilter === "rejected" ? "preset-filled-primary-500" : "preset-tonal-surface"}`} aria-pressed={decisionFilter === "rejected"} onclick={() => selectDecisionFilter("rejected")}>Passed</button>
              <button type="button" class={`btn btn-sm min-h-10 ${decisionFilter === "read" ? "preset-filled-primary-500" : "preset-tonal-surface"}`} aria-pressed={decisionFilter === "read"} onclick={() => selectDecisionFilter("read")}>Read ({data.readCount})</button>
            </div>
          </section>
        {/if}
        {#if view !== "decisions" || decisionFilter !== "read"}
        {#if view === "discover" && digestVisible}
          <section
            in:fade={{ duration: motionDuration(260) }}
            class="mb-6 flex flex-col gap-4 border border-secondary-500/25 preset-tonal-secondary p-4 sm:flex-row sm:items-center sm:justify-between"
            aria-label="Digest review"
          >
            <div class="flex items-start gap-3">
              <span class="grid size-10 shrink-0 place-items-center preset-filled-secondary-500"><Bell size={18} /></span>
              <div>
                <p class="font-semibold text-surface-950-50">Your weekly picks are ready</p>
                <p class="mt-1 text-sm leading-6 text-surface-700-300">Select the books you want to keep, then add them to your shortlist in one step.</p>
              </div>
            </div>
            <div class="flex flex-wrap gap-2 sm:shrink-0">
              <button type="button" class="btn btn-sm min-h-10 preset-tonal-secondary" onclick={() => selectVisibleDigestBooks(viewVisibleBooks)}>Select all visible</button>
              {#if selectedDigestCount > 0}
                <form method="POST" action="?/shortlistBulk" use:enhance={setPendingDigestBulk()}>
                  <ProfileField profileId={data.user?.profile_id} />
                  {#if selectedDigestRunId}<input type="hidden" name="run_id" value={selectedDigestRunId} />{/if}
                  {#each [...selectedDigestIds] as id (id)}<input type="hidden" name="ids" value={id} />{/each}
                  <button type="submit" class="btn btn-sm min-h-10 preset-filled-secondary-500" disabled={isPending("shortlist-bulk")} aria-busy={isPending("shortlist-bulk")}>
                    {#if isPending("shortlist-bulk")}<RefreshCw size={15} class="animate-spin" />{:else}<Bookmark size={15} />{/if} Add {selectedDigestCount} to shortlist
                  </button>
                </form>
                <button type="button" class="btn btn-sm min-h-10 preset-tonal-surface" onclick={clearDigestSelection}>Clear</button>
              {/if}
              <button type="button" class="btn btn-sm min-h-10 preset-tonal-surface" onclick={exitDigestMode}>View all</button>
            </div>
          </section>
        {/if}
        <section
          class="grid gap-3 sm:gap-4 lg:grid-cols-2 lg:items-start"
          aria-live="polite"
        >
          {#each viewVisibleBooks as book, index (book.id)}
            {@const releaseLabel = formatRelease(book.published_on, book.published_kind)}
            {@const decisionLabel = book.status === "maybe_later" ? "Maybe later" : book.status === "rejected" ? "Passed" : "Read"}
            {@const decisionVariant = book.status === "maybe_later" ? "preset-tonal-warning" : book.status === "rejected" ? "preset-tonal-error" : "preset-tonal-success"}
            <article
              use:trackRecommendation={{ candidateId: book.id }}
              in:fly={{ y: 18, duration: motionDuration(380), delay: motionDelay(index) }}
              out:fade={{ duration: motionDuration(160) }}
              animate:flip={{ duration: motionDuration(360) }}
              class={`relative isolate card group grid min-w-0 grid-cols-1 bg-gradient-to-br from-primary-500/8 via-transparent to-secondary-500/8 ${view === "decisions" ? decisionVariant : "preset-tonal-surface"} shadow-lg shadow-primary-500/5 transition duration-300 hover:-translate-y-1 hover:shadow-2xl hover:shadow-primary-500/10 ${openReadMenuId === book.id ? "z-30 overflow-visible" : "overflow-hidden"} sm:grid-cols-[10rem_minmax(0,1fr)] ${index === 0 && view === "discover" ? "lg:col-span-2 lg:grid-cols-[12rem_minmax(0,1fr)]" : ""}`}
            >
              <div
                class={`${book.cover_url ? "pointer-events-none absolute inset-0 z-0" : "hidden"} aspect-[2/3] h-full w-full self-start overflow-hidden rounded-[inherit] sm:pointer-events-auto sm:relative sm:inset-auto sm:z-auto sm:block sm:h-auto sm:rounded-none sm:rounded-tl-[inherit] sm:preset-tonal-surface sm:before:pointer-events-none sm:before:absolute sm:before:inset-0 sm:before:z-10 sm:before:bg-gradient-to-t sm:before:from-primary-950/30 sm:before:via-transparent sm:before:to-secondary-500/10 sm:before:content-['']`}
              >
                <span class="absolute inset-0 hidden flex-col items-center justify-center gap-3 p-4 text-center text-surface-600-400 sm:flex" aria-hidden="true">
                  <BookOpen size={28} /><span class="line-clamp-3 text-xs">{book.title}</span>
                </span>
                {#if book.cover_url}<img
                    in:fade={{ duration: motionDuration(340) }}
                    class="absolute inset-0 h-full w-full scale-105 object-cover opacity-75 blur-[2px] sm:scale-100 sm:opacity-100 sm:blur-none sm:transition sm:duration-500 sm:group-hover:scale-[1.05]"
                    src={book.cover_url}
                    alt=""
                    width="160"
                    height="240"
                    loading={index > 2 ? "lazy" : "eager"}
                    onerror={(event) => ((event.currentTarget as HTMLImageElement).hidden = true)}
                  />{/if}
                <div class="pointer-events-none absolute inset-0 preset-filled-surface-50-950 opacity-[0.82] sm:hidden" aria-hidden="true"></div>
                <div class="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary-500/10 via-transparent to-secondary-500/15 sm:hidden" aria-hidden="true"></div>
                <button
                  type="button"
                  class="hidden text-left sm:absolute sm:inset-0 sm:z-20 sm:block focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500"
                  onclick={() => toggleRecommendationDetails(book.id)}
                  aria-label={`View details for ${book.title}`}
                  aria-expanded={expandedRecommendationDetails.has(book.id)}
                >
                  <span in:scale={{ duration: motionDuration(160) }} class="badge absolute bottom-3 left-3 preset-filled-primary-500" title="Relative ranking score, not a probability or star rating">Rank {book.score}</span>
                </button>
              </div>
              <div class="z-10 flex min-w-0 flex-col gap-2 p-3 sm:z-auto sm:gap-2.5 sm:p-4">
              <div class="flex flex-wrap items-center gap-2 text-xs font-medium text-surface-600-400 sm:text-sm">
                  {#if view === "discover" && digestVisible && book.status === "recommended"}
                    <label class="grid size-8 shrink-0 cursor-pointer place-items-center preset-filled-surface-50-950 opacity-90 shadow-lg sm:absolute sm:left-3 sm:top-3 sm:z-30" title={`Select ${book.title}`}>
                      <input
                        class="checkbox size-6 accent-secondary-500"
                        type="checkbox"
                        checked={selectedDigestIds.has(book.id)}
                        onchange={() => toggleDigestBook(book.id)}
                        aria-label={`Select ${book.title} for shortlist`}
                      />
                    </label>
                  {/if}
                  <span class="badge badge-sm preset-filled-primary-500 sm:hidden" title="Relative ranking score, not a probability or star rating">Rank {book.score}</span>
                  {#if view === "decisions"}<span class={`badge badge-sm ${decisionVariant}`}>{decisionLabel}</span>{/if}
                  {#if book.status === "imported"}<span class="badge badge-sm preset-tonal-success">Added to Librarr</span>{/if}
                  {#if releaseLabel}<span>{releaseLabel}</span>{/if}
                  {#each book.genres.slice(0, 2) as genre (genre)}{@const genrePressed = viewGenre.trim().toLowerCase() === genre.trim().toLowerCase()}<ChipToggle pressed={genrePressed} label={genre} ariaLabel={`${genre} filter`} onToggle={(next) => setGenreForView(view, next ? canonicalGenreForView(view, genre) : "")} />{/each}
                  {#if typeof book.metadata_confidence === "number" && book.metadata_confidence < 0.65}<span class="badge badge-sm preset-tonal-warning" title="Sparse catalog details reduced this recommendation's score">Limited metadata</span>{/if}
                  {#if book.source_url}<a
                      in:scale={{ duration: motionDuration(180) }}
                      class="btn btn-icon btn-xs min-h-8 min-w-8 preset-tonal-surface shrink-0"
                      href={book.source_url}
                      target="_blank"
                      rel="noreferrer"
                      onclick={() => recordBookEvent(book.id, "source_open")}
                      aria-label={`Open source for ${book.title}`}><ExternalLink size={13} /></a
                    >{/if}
                </div>
                <div>
                  <h2 class="text-2xl font-semibold tracking-tight text-surface-950-50 sm:text-3xl">{book.title}</h2>
                  <p class="mt-1 text-base text-surface-700-300">{book.author}</p>
                </div>
                <details
                  class="group/details text-base text-surface-700-300"
                  open={expandedRecommendationDetails.has(book.id)}
                  ontoggle={(event) => {
                    const open = (event.currentTarget as HTMLDetailsElement).open;
                    if (open) recordBookEvent(book.id, "detail_open");
                    const isExpanded = expandedRecommendationDetails.has(book.id);
                    if (open !== isExpanded) toggleRecommendationDetails(book.id);
                  }}
                >
                  <summary class="flex min-h-10 cursor-pointer list-none items-center gap-2 font-medium text-primary-600-400">
                    <Sparkles size={15} /><span>Why this might be for you</span><ChevronDown size={15} class="ml-auto transition group-open/details:rotate-180" />
                  </summary>
                  {#if expandedRecommendationDetails.has(book.id)}<div in:slide={{ duration: motionDuration(220) }} out:fade={{ duration: motionDuration(120) }}>
                      <p class="mt-2 leading-6">{book.reason}</p>
                      {#if book.synopsis}<div class="mt-4 border-l-2 border-secondary-500/50 pl-3">
                          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-600-400">Book summary</p>
                          <p class="mt-1 leading-6 text-surface-800-200">{book.synopsis}</p>
                        </div>{/if}
                    </div>{/if}
                </details>
                <div class="mt-auto flex flex-wrap items-center gap-2 pt-0.5">
                  {#if book.status === "recommended"}
                    {#if librarrConnected}<button in:fly={{ y: 8, duration: motionDuration(180) }} type="button" class="btn btn-sm min-h-10 preset-tonal-secondary" aria-label={`Find in Librarr: ${book.title} by ${book.author}`} onclick={() => openLibrarrSearch(book)}><Search size={15} /> Find in Librarr</button>{/if}
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(1, 20) }} method="POST" action="?/decide" use:enhance={setPending(`save-${book.id}`, optimisticDecision)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="saved" /><input type="hidden" name="run_id" value={book.recommendation_run_id ?? ""} /><button type="submit" class="btn btn-sm min-h-10 preset-filled-primary-500" aria-label={`Shortlist: ${book.title} by ${book.author}`} aria-busy={isPending(`save-${book.id}`)}>{#if isPending(`save-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<Bookmark size={15} />{/if} Shortlist</button>
                    </form>
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(2, 20) }} method="POST" action="?/decide" use:enhance={setPending(`pass-${book.id}`, optimisticDecision)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="rejected" /><input type="hidden" name="run_id" value={book.recommendation_run_id ?? ""} /><button type="submit" class="btn btn-icon btn-sm min-h-10 min-w-10 preset-tonal-surface" aria-label={`Pass on ${book.title}`} title="Pass" aria-busy={isPending(`pass-${book.id}`)}>{#if isPending(`pass-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<ThumbsDown size={16} />{/if}</button>
                    </form>
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(3, 20) }} method="POST" action="?/decide" use:enhance={setPending(`later-${book.id}`, optimisticDecision)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="maybe_later" /><input type="hidden" name="run_id" value={book.recommendation_run_id ?? ""} /><button type="submit" class="btn btn-icon btn-sm min-h-10 min-w-10 preset-tonal-surface" aria-label={`Maybe later on ${book.title}`} title="Maybe later" aria-busy={isPending(`later-${book.id}`)}>{#if isPending(`later-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<Clock size={16} />{/if}</button>
                    </form>
                    <div class="relative z-50 ml-auto shrink-0" data-read-menu-root={book.id}>
                      <button
                        type="button"
                        class="btn btn-icon btn-xs min-h-8 min-w-8 preset-tonal-surface"
                        aria-label={`More options for ${book.title}`}
                        data-read-menu-trigger={book.id}
                        aria-expanded={openReadMenuId === book.id}
                        title="More options"
                        onclick={() => (openReadMenuId = openReadMenuId === book.id ? null : book.id)}
                      ><EllipsisVertical size={15} /></button>
                      {#if openReadMenuId === book.id}<div
                        in:fly={{ y: 8, duration: motionDuration(180) }}
                        out:fly={{ y: 8, duration: motionDuration(140) }}
                        class="absolute bottom-full right-0 z-50 mb-2 w-56 border preset-filled-surface-50-950 p-3 text-left normal-case shadow-xl"
                      >
                        <p class="text-sm font-semibold text-surface-950-50">Already read this?</p>
                        <button
                          type="button"
                          class="btn btn-sm mt-3 min-h-10 w-full preset-filled-primary-500"
                          aria-label={`Mark Read: ${book.title} by ${book.author}`}
                          onclick={() => openMarkRead(book)}
                        ><BookOpen size={14} /> Mark Read</button>
                      </div>{/if}
                    </div>
                  {:else if book.status === "saved" || book.status === "imported"}
                    {#if librarrConnected}
                      <button in:fly={{ y: 8, duration: motionDuration(180) }} type="button" class="btn btn-sm min-h-10 preset-tonal-secondary" aria-label={`Find in Librarr: ${book.title} by ${book.author}`} onclick={() => openLibrarrSearch(book)}><Search size={15} /> Find in Librarr</button>
                      {#if book.status === "saved"}
                        <form in:fly={{ y: 8, duration: motionDuration(180) }} method="POST" action="?/importLibrar" use:enhance={setPending(`import-${book.id}`, optimisticImport)}>
                          <ProfileField profileId={data.user?.profile_id} />
                          <input type="hidden" name="id" value={book.id} /><input type="hidden" name="run_id" value={book.recommendation_run_id ?? ""} /><button type="submit" class="btn btn-sm min-h-10 preset-filled-primary-500" aria-label={`Add ${configuredLibrarrMediaType === "ebook" ? "ebook" : "audiobook"} to waitlist: ${book.title} by ${book.author}`} disabled={isPending(`import-${book.id}`)} aria-busy={isPending(`import-${book.id}`)}>{#if isPending(`import-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<Library size={15} />{/if} Add {configuredLibrarrMediaType === "ebook" ? "ebook" : "audiobook"} to waitlist</button>
                        </form>
                      {/if}
                    {:else if book.status === "saved"}
                      <button type="button" class="btn btn-sm min-h-10 preset-tonal-secondary" onclick={goToLibrarrSettings}><Library size={15} /> Set up Librarr</button>
                    {/if}
                    <button
                      in:fly={{ y: 8, duration: motionDuration(180) }}
                      type="button"
                      class="btn btn-sm min-h-10 preset-filled-primary-500"
                      aria-label={`Mark Read: ${book.title} by ${book.author}`}
                      onclick={() => openMarkRead(book)}
                    ><Check size={15} /> Mark Read</button>
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(2, 20) }} method="POST" action="?/decide" use:enhance={setPending(`restore-${book.id}`, optimisticDecision)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="recommended" /><input type="hidden" name="run_id" value={book.recommendation_run_id ?? ""} /><button type="submit" class="btn btn-sm min-h-10 preset-tonal-surface" aria-label={`Remove ${book.title} from your shortlist`} aria-busy={isPending(`restore-${book.id}`)}>Remove</button>
                    </form>
                  {:else if book.status === "rejected" || book.status === "maybe_later"}
                    <form in:fly={{ y: 8, duration: motionDuration(180) }} method="POST" action="?/decide" use:enhance={setPending(`restore-${book.id}`, optimisticDecision)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="recommended" /><button type="submit" class="btn btn-sm min-h-10 preset-filled-primary-500" aria-label={`Return to Discover: ${book.title}`} aria-busy={isPending(`restore-${book.id}`)}>{#if isPending(`restore-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<ArrowRight size={15} />{/if} Return to Discover</button>
                    </form>
                  {/if}
                </div>
              </div>
            </article>
          {:else}
            {#if viewInitialLoading}<div class="card col-span-full flex min-h-48 items-center justify-center gap-3 preset-tonal-surface p-8 text-sm text-surface-700-300" role="status">
                <RefreshCw size={17} class="animate-spin" />{view === "decisions" ? "Loading past decisions…" : "Searching recommendations…"}
              </div>
            {:else if viewFilterError}<div class="card col-span-full flex min-h-48 flex-col items-center justify-center gap-3 preset-tonal-surface p-8 text-center" role="alert">
                <p class="text-sm text-surface-700-300">{viewFilterError}</p>
                <button type="button" class="btn btn-sm min-h-10 preset-filled-primary-500" onclick={() => (view === "decisions" ? reloadQueriedDecisions() : reloadFilteredDiscover())}><RefreshCw size={14} /> Try again</button>
              </div>
            {:else}<div in:scale={{ duration: motionDuration(260) }} class="card col-span-full flex min-h-72 flex-col items-center justify-center gap-4 border-dashed preset-tonal-surface p-8 text-center">
                <span class="grid size-14 place-items-center rounded-full preset-tonal-primary"><BookOpen size={24} /></span>
                <div>
                  <h2 class="text-lg font-semibold text-surface-950-50">{view === "saved" && shortlistTotalCount === 0 ? "Your shortlist is empty" : view === "saved" && viewHasFilter ? "No shortlist books match these filters" : viewHasFilter ? "No books match these filters" : view === "decisions" && decisionFilter === "maybe_later" ? "No deferred books" : view === "decisions" && decisionFilter === "rejected" ? "No passed books" : view === "decisions" ? "No past decisions" : digestRequested ? "No unread digest picks left" : digestVisible ? "No digest picks yet" : "You are all caught up"}</h2>
                  <p class="mt-1 max-w-sm text-sm leading-6 text-surface-700-300">{view === "saved" && shortlistTotalCount === 0 ? "Shortlist a recommendation when one catches your eye." : view === "saved" && viewHasFilter ? "Change the shortlist search or genre filter to see your saved books." : viewHasFilter ? "Change the search or genre filter to see more books." : view === "decisions" && decisionFilter === "maybe_later" ? "Books you set aside with Maybe later will appear here." : view === "decisions" && decisionFilter === "rejected" ? "Books you pass on will appear here." : view === "decisions" ? "Books you pass on or set aside for later will appear here." : digestRequested ? "You have already acted on the books from this digest, or open View all to browse Discover." : digestVisible ? "The next digest will appear here when a new book clears your match threshold." : "Refresh your sources or check back when your next set of books is ready."}</p>
                </div>
                {#if view === "saved" && shortlistTotalCount === 0}<button in:fly={{ y: 8, duration: motionDuration(220) }} type="button" class="btn preset-filled-primary-500" onclick={() => go("discover")}>Browse recommendations <ArrowRight size={16} /></button>{/if}
                {#if view === "discover" && !viewHasFilter && !digestVisible && !effectiveDiscoverHasMore && !discoverLoadError}<form method="POST" action="?/runSync" use:enhance={setPending("sync")}>
                  <ProfileField profileId={data.user?.profile_id} />
                  <button type="submit" class="btn preset-filled-primary-500" disabled={isPending("sync")} aria-busy={isPending("sync")}>
                    {#if isPending("sync")}<RefreshCw size={15} class="animate-spin" />{:else}<RefreshCw size={15} />{/if} Refresh recommendations
                  </button>
                </form>{/if}
              </div>{/if}{/each}
          {#if viewHasMoreDiscoverBooks}<div bind:this={loadMoreSentinel} class="col-span-full flex flex-col items-center gap-2 pt-1">
              {#if discoverLoadError}<p class="text-sm text-error-600-400" role="alert">{discoverLoadError}</p>{/if}
              <button type="button" class="btn btn-sm preset-tonal-secondary" onclick={loadMoreDiscover} disabled={discoverLoading} aria-busy={discoverLoading}>{#if discoverLoading}<RefreshCw size={14} class="animate-spin" />{:else}<ArrowRight size={14} />{/if} {discoverLoadError ? "Try again" : "Load more"}</button>
            </div>{/if}
          {#if view === "decisions" && decisionFilter !== "read" && decisionsHasMore}<div class="col-span-full flex flex-col items-center gap-2 pt-1">
              {#if decisionsLoadError}<p class="text-sm text-error-600-400" role="alert">{decisionsLoadError}</p>{/if}
              <button type="button" class="btn btn-sm preset-tonal-secondary" onclick={loadMoreDecisions} disabled={decisionsLoading} aria-busy={decisionsLoading}>
                {#if decisionsLoading}<RefreshCw size={14} class="animate-spin" />{:else if decisionsLoadError}<RefreshCw size={14} />{:else}<ArrowRight size={14} />{/if} {decisionsLoadError ? "Try again" : "Load more decisions"}
              </button>
            </div>{/if}
        </section>
        {:else}
          <section
            aria-label="Read books"
            aria-busy={readingHistoryState === "idle" || readingHistoryState === "loading"}
            class="min-w-0"
          >
            {#if readingHistoryState === "ready"}
              <div class="mb-5 grid gap-3 sm:grid-cols-3" role="group" aria-label="Filter and sort read books">
                <label class="block text-sm font-medium text-surface-800-200">
                  Rating
                  <select bind:value={readingHistoryRatingFilter} class="select mt-2 min-h-10 w-full preset-tonal-surface" aria-label="Filter read books by rating">
                    <option value="all">Any rating</option>
                    <option value="rated">Rated</option>
                    <option value="unrated">Unrated</option>
                  </select>
                </label>
                <label class="block text-sm font-medium text-surface-800-200">
                  Source
                  <select bind:value={readingHistorySourceFilter} class="select mt-2 min-h-10 w-full preset-tonal-surface" aria-label="Filter read books by source">
                    <option value="all">All sources</option>
                    {#each readingHistorySources as source (source)}
                      <option value={source}>{readingSourceLabel(source)}</option>
                    {/each}
                  </select>
                </label>
                <label class="block text-sm font-medium text-surface-800-200">
                  Sort by
                  <select bind:value={readingHistorySort} class="select mt-2 min-h-10 w-full preset-tonal-surface" aria-label="Sort read books">
                    <option value="recent">Most recently read</option>
                    <option value="oldest">Oldest read</option>
                    <option value="score_high">Highest Bookward score</option>
                    <option value="score_low">Lowest Bookward score</option>
                    <option value="rating_high">Highest rating</option>
                    <option value="title">Title A–Z</option>
                  </select>
                </label>
              </div>
            {/if}
            {#if readingHistoryScoreState === "loading"}
              <p class="mb-4 text-sm text-surface-700-300" role="status" aria-live="polite">
                Calculating Bookward scores for {scoredReadingHistoryCount} of {readingHistory.length} read books…
              </p>
            {:else if readingHistoryScoreState === "error"}
              <div class="mb-4 flex flex-wrap items-center gap-3 text-sm text-surface-700-300" role="status">
                <span>{readingHistoryScoreError} Scores loaded for {scoredReadingHistoryCount} of {readingHistory.length} books.</span>
                <button type="button" class="btn btn-sm preset-tonal-secondary" onclick={() => void loadReadingHistoryScores(readingHistory.length)}>Retry scoring</button>
              </div>
            {/if}
            {#if readingHistoryState === "idle" || readingHistoryState === "loading"}
              <div class="card flex min-h-48 items-center justify-center gap-3 preset-tonal-surface p-8 text-sm text-surface-700-300" role="status"><RefreshCw size={17} class="animate-spin" />Loading your read books…</div>
            {:else if readingHistoryState === "error"}
              <div class="card flex min-h-48 flex-col items-center justify-center gap-3 preset-tonal-surface p-8 text-center" role="alert">
                <p class="text-sm text-surface-700-300">{readingHistoryError}</p>
                <button type="button" class="btn btn-sm min-h-10 preset-filled-primary-500" onclick={() => void loadReadingHistory()}><RefreshCw size={14} /> Try again</button>
              </div>
            {:else if readingHistoryState === "ready" && filteredReadingHistory.length > 0}
              <p class="mb-3 text-xs text-surface-700-300">Showing {visibleReadingHistory.length} of {filteredReadingHistory.length} books</p>
              <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {#each visibleReadingHistory as item (item.id)}
                  <article class="card min-w-0 preset-tonal-success p-4">
                    <div class="flex items-start gap-3">
                      <span class="grid size-9 shrink-0 place-items-center preset-tonal-primary"><BookOpen size={16} /></span>
                      <div class="min-w-0 flex-1">
                        <h2 class="line-clamp-2 font-semibold leading-5 text-surface-950-50">{item.title}</h2>
                        <p class="mt-1 truncate text-sm text-surface-700-300">{item.author}</p>
                        <p class="mt-2 text-xs text-surface-700-300">Bookward score <span class="font-semibold text-surface-950-50">{item.algorithm_score === null ? readingHistoryScoreState === "loading" ? "Calculating…" : "Unavailable" : `${item.algorithm_score.toFixed(1)} / 100`}</span></p>
                      </div>
                      <div class="flex shrink-0 flex-col items-end gap-2">
                        <span class="badge badge-sm preset-tonal-success">Read</span>
                        {#if item.rating !== null && item.rating > 0}
                          <StarRatingBadge rating={item.rating} />
                        {:else}
                          <span class="badge preset-tonal-surface">Unrated</span>
                        {/if}
                      </div>
                    </div>
                    <details
                      class="group/details mt-3 border-t border-surface-300-700/40 pt-2"
                      open={expandedReadingDetails.includes(item.id)}
                      ontoggle={(event) => {
                        const open = (event.currentTarget as HTMLDetailsElement).open;
                        const isExpanded = expandedReadingDetails.includes(item.id);
                        if (open !== isExpanded) toggleReadingDetails(item.id);
                      }}
                    >
                      <summary
                        class="flex min-h-9 cursor-pointer list-none items-center gap-2 text-left text-sm font-medium text-primary-600-400 [&::-webkit-details-marker]:hidden"
                        aria-label={`Reading details for ${item.title} by ${item.author}`}
                      >
                        <span>Reading details</span>
                        <ChevronDown size={15} aria-hidden="true" class="ml-auto transition group-open/details:rotate-180" />
                      </summary>
                      <div class="pt-2">
                        <dl class="grid gap-2 text-xs">
                          <div class="flex justify-between gap-3"><dt class="text-surface-600-400">Imported from</dt><dd class="text-right text-surface-800-200">{readingSourceLabel(item.source)}</dd></div>
                          <div class="flex justify-between gap-3"><dt class="text-surface-600-400">Read date</dt><dd class="text-right text-surface-800-200">{formatHistoryDate(item.read_at)}</dd></div>
                          <div class="flex justify-between gap-3"><dt class="text-surface-600-400">Added to Bookward</dt><dd class="text-right text-surface-800-200">{formatHistoryDate(item.created_at)}</dd></div>
                        </dl>
                        <div class="mt-3 border-t border-surface-300-700/40 pt-3 text-xs leading-5">
                          {#if item.algorithm_score !== null}
                            <p class="font-semibold text-surface-950-50">Current profile score: {item.algorithm_score.toFixed(1)} / 100</p>
                            <p class="mt-1 text-surface-700-300">This relative score compares the book with your other rated reads, excluding this title to avoid a self-match. It is not a probability or star rating.</p>
                            {#if item.algorithm_explanation.length}
                              <ul class="mt-2 list-disc space-y-1 pl-4 text-surface-700-300">
                                {#each item.algorithm_explanation as reason (reason)}<li>{reason}</li>{/each}
                              </ul>
                            {:else}
                              <p class="mt-2 text-surface-700-300">There are not yet other rated reads to compare with this title.</p>
                            {/if}
                          {:else}
                            <p class="text-surface-700-300">Bookward could not calculate a current profile score. Try again later.</p>
                          {/if}
                          {#if item.rank_score !== null}
                            <p class="mt-3 border-t border-surface-300-700/40 pt-3 font-semibold text-surface-950-50">Score when Bookward last recommended it: {item.rank_score.toFixed(1)} / 100</p>
                          {:else}
                            <p class="mt-3 border-t border-surface-300-700/40 pt-3 text-surface-700-300">Bookward had not previously recommended this title.</p>
                          {/if}
                        </div>
                      </div>
                    </details>
                  </article>
                {/each}
              </div>
              {#if visibleReadingHistory.length < filteredReadingHistory.length}
                <div class="mt-5 flex justify-center"><button type="button" class="btn btn-sm min-h-10 preset-tonal-secondary" onclick={() => (readingHistoryVisibleCount += 30)}><ArrowRight size={14} /> Load more books</button></div>
              {/if}
            {:else}
              <div class="card flex min-h-64 flex-col items-center justify-center gap-3 border-dashed preset-tonal-surface p-8 text-center">
                <span class="grid size-12 place-items-center rounded-full preset-tonal-primary"><BookOpen size={22} /></span>
                <div>
                  <h2 class="font-semibold text-surface-950-50">{decisionsSearch || readingHistoryRatingFilter !== "all" || readingHistorySourceFilter !== "all" ? "No read books match these filters" : "No read books yet"}</h2>
                  <p class="mt-1 max-w-md text-sm leading-6 text-surface-700-300">{decisionsSearch || readingHistoryRatingFilter !== "all" || readingHistorySourceFilter !== "all" ? "Change the search, rating, or source filter to see more books." : "Import a Goodreads or StoryGraph CSV, or a Goodreads read-shelf feed, in Settings to see your reading history here."}</p>
                </div>
              </div>
            {/if}
          </section>
        {/if}
      {/snippet}

      <!-- Layer each view in the same grid cell so fade intros/outros overlap instead of leaving a blank frame. -->
      <div class="grid min-h-[20rem] min-w-0">

        {#if activeView === "discover"}
          <div
            data-view="recommendations"
            class="col-start-1 row-start-1"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            {@render recommendationView("discover")}
          </div>
        {:else if activeView === "saved"}
          <div
            data-view="recommendations"
            class="col-start-1 row-start-1"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            {@render recommendationView("saved")}
          </div>
        {:else if activeView === "decisions"}
          <div
            data-view="recommendations"
            class="col-start-1 row-start-1"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            {@render recommendationView("decisions")}
          </div>
        {:else if activeView === "sources"}
          <div
            data-view="sources"
            class="col-start-1 row-start-1 min-w-0 w-full"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            <section class="mb-6 min-w-0 max-w-2xl">
              <h1
                class="text-4xl font-semibold tracking-tight text-surface-950-50 sm:text-5xl"
              >
                Choose your sources.
              </h1>
            </section>
            <section
              class="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1.5fr)_minmax(18rem,1fr)]"
            >
              <div
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(0),
                }}
                class="card min-w-0 preset-tonal-surface p-4 sm:p-6"
              >
                <div class="mb-4 flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <h2 class="font-semibold text-surface-950-50">
                      Available sources
                    </h2>
                    <p class="mt-1 text-sm text-surface-700-300">
                      {activeSourceCount} active source{activeSourceCount === 1
                        ? ""
                        : "s"}
                    </p>
                  </div>
                  <div
                    class="flex min-w-0 w-full items-center gap-1 preset-tonal-surface p-1 sm:w-auto"
                    role="tablist"
                    aria-label="Filter sources by lifecycle"
                  >
                    {#each sourceFilterOptions as option}
                      <button
                        type="button"
                        role="tab"
                        id={`source-tab-${option.value}`}
                        aria-controls="source-filter-panel"
                        aria-selected={sourceFilter === option.value}
                        tabindex={sourceFilter === option.value ? 0 : -1}
                        class={`min-h-9 min-w-0 flex-1 break-words px-2 text-center text-xs font-medium leading-tight transition sm:flex-none sm:px-3 ${sourceFilter === option.value ? "preset-filled-secondary-500" : "text-surface-700-300 hover:preset-tonal-secondary"}`}
                        onclick={() => (sourceFilter = option.value)}
                        onkeydown={(event) => {
                          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                          event.preventDefault();
                          const current = sourceFilterOptions.findIndex((item) => item.value === option.value);
                          const next = event.key === "Home" ? 0 : event.key === "End" ? sourceFilterOptions.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + sourceFilterOptions.length) % sourceFilterOptions.length;
                          sourceFilter = sourceFilterOptions[next].value;
                          document.getElementById(`source-tab-${sourceFilterOptions[next].value}`)?.focus();
                        }}
                      >
                        {option.label}
                        <span class="ml-1 opacity-70">{option.count}</span>
                      </button>
                    {/each}
                  </div>
                </div>
                <div id="source-filter-panel" role="tabpanel" aria-labelledby={`source-tab-${sourceFilter}`} tabindex="0" class="min-w-0 divide-y divide-surface-200-800">
                  {#if sourceFilter === "all"}
                    <div
                      in:fly={{ x: -10, duration: motionDuration(240) }}
                      class="flex min-h-24 items-center gap-4 py-4"
                    >
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"
                      ><Sparkles size={18} /></span
                      >
                      <div class="min-w-0 flex-1">
                        <h3 class="font-medium text-surface-950-50">
                          Bookward demo books
                        </h3>
                        <p class="mt-1 text-sm text-surface-700-300">
                          A small offline sample to test a fresh installation.
                        </p>
                        <span class="mt-1 block text-xs text-surface-600-400"
                          >Bundled sample</span
                        >
                      </div>
                      <form
                        method="POST"
                        action="?/toggleDefault"
                        use:enhance={setPending("toggle-default", () => optimisticDefaultToggle())}
                      >
                        <ProfileField profileId={data.user?.profile_id} />
                        <button
                          type="submit"
                          class={`relative h-7 w-12 rounded-full p-1 transition ${defaultSourceEnabled ? "preset-filled-primary-500" : "preset-filled-surface-500"}`}
                          role="switch"
                          aria-label="Toggle Bookward demo books"
                          aria-checked={defaultSourceEnabled}
                          disabled={isPending("toggle-default")}
                          aria-busy={isPending("toggle-default")}
                          ><span
                            class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition ${defaultSourceEnabled ? "translate-x-5" : ""}`}
                          ></span></button
                        >
                      </form>
                    </div>
                  {/if}
                  {#key sourceFilter}
                    <div in:fade={{ duration: motionDuration(160) }} out:fade={{ duration: motionDuration(100) }}>
                  {#each visibleSourceGroups as group}
                    <div
                      class="flex min-w-0 items-start justify-between gap-3 border-b border-surface-200-800 px-1 py-4 sm:items-end sm:gap-4"
                    >
                      <div class="min-w-0 flex-1">
                        <h3 class="font-semibold text-surface-950-50">
                          {group.label}
                        </h3>
                        <p class="mt-1 break-words text-sm text-surface-700-300">
                          {group.description}
                        </p>
                      </div>
                      <span class="badge max-w-[45%] shrink-0 whitespace-normal text-right leading-tight preset-tonal-surface"
                        >{group.active} active · {group.sources.length} total</span
                      >
                    </div>
                    {#if group.sources.length}
                      {#each group.sources as source (source.id)}<div
                          in:fly={{
                            x: -10,
                            duration: motionDuration(240),
                            delay: motionDelay(source.id % 5, 24),
                          }}
                          out:fade={{ duration: motionDuration(120) }}
                          class="flex min-h-24 min-w-0 items-center gap-3 border-b border-surface-200-800 py-4 last:border-b-0 sm:gap-4"
                        >
                          <span
                            class={`grid size-10 shrink-0 place-items-center ${source.lifecycle === "one_time" ? "preset-tonal-tertiary" : "preset-tonal-secondary"}`}
                            ><Link2 size={18} /></span
                          >
                          <div class="min-w-0 flex-1">
                            <h3 class="truncate font-medium text-surface-950-50">
                              {source.label}
                            </h3>
                            <p class="mt-1 truncate text-sm text-surface-700-300">
                              {source.url}
                            </p>
                            <span class="mt-1 block break-words text-xs text-surface-600-400">
                              {#if source.lifecycle === "one_time"}
                                One-time import · {formatSourceScan(source.last_scanned_at, source.last_status)}
                              {:else}
                                Permanent feed
                                {#if source.kind !== "association" && source.kind !== "builtin"}
                                  <span aria-hidden="true"> · </span>
                                  <form
                                    class="inline-flex"
                                    method="POST"
                                    action="?/switchSourceToOneTime"
                                    use:enhance={setPending(`source-lifecycle-${source.id}`)}
                                  >
                                    <ProfileField profileId={data.user?.profile_id} />
                                    <input type="hidden" name="id" value={source.id} />
                                    <button
                                      class="inline-flex min-h-8 items-center text-left font-medium text-secondary-600-400 underline decoration-secondary-500/50 underline-offset-2 hover:text-secondary-500-300 disabled:opacity-50"
                                      type="submit"
                                      disabled={isPending(`source-lifecycle-${source.id}`)}
                                      aria-busy={isPending(`source-lifecycle-${source.id}`)}
                                    >Switch to One-Time Import</button>
                                  </form>
                                {/if}
                                <span aria-hidden="true"> · </span>
                                {formatSourceScan(source.last_scanned_at, source.last_status)}
                              {/if}
                            </span>
                            <details class="mt-2 w-full max-w-full">
                              <summary class="min-h-8 cursor-pointer py-2 text-xs font-medium text-secondary-600-400 hover:text-secondary-500-300">
                                Filters{source.filters.include_genres.length || source.filters.exclude_genres.length
                                  ? ` · ${source.filters.include_genres.length + source.filters.exclude_genres.length} set`
                                  : ""}
                              </summary>
                              <form
                                class="mt-3 w-full min-w-0 space-y-3 border-l-2 border-secondary-500/30 pl-3"
                                method="POST"
                                action="?/configureSourceFilters"
                                use:enhance={setPending(`source-filters-${source.id}`)}
                              >
                                <ProfileField profileId={data.user?.profile_id} />
                                <input type="hidden" name="id" value={source.id} />
                                <label class="block text-xs font-medium text-surface-800-200">
                                  Include genres
                                  <input
                                    class="input mt-1 w-full min-w-0 max-w-full text-sm"
                                    name="includeGenres"
                                    value={source.filters.include_genres.join(", ")}
                                    placeholder="science fiction, fantasy"
                                  />
                                </label>
                                <label class="block text-xs font-medium text-surface-800-200">
                                  Exclude genres
                                  <input
                                    class="input mt-1 w-full min-w-0 max-w-full text-sm"
                                    name="excludeGenres"
                                    value={source.filters.exclude_genres.join(", ")}
                                    placeholder="romance, cookbook"
                                  />
                                </label>
                                <p class="text-xs leading-5 text-surface-600-400">
                                  Separate genres with commas. An include filter requires a matching genre tag; untagged books remain eligible with exclusions alone.
                                </p>
                                <button
                                  class="btn btn-sm min-h-9 preset-tonal-secondary"
                                  type="submit"
                                  disabled={isPending(`source-filters-${source.id}`)}
                                  aria-busy={isPending(`source-filters-${source.id}`)}
                                >{#if isPending(`source-filters-${source.id}`)}<RefreshCw size={14} class="animate-spin" />{:else}<Check size={14} />{/if} Save filters</button>
                              </form>
                            </details>
                          </div>
                          <form
                            class="shrink-0"
                            method="POST"
                            action="?/toggleSource"
                            use:enhance={setPending(`toggle-source-${source.id}`, optimisticSourceToggle)}
                          >
                            <ProfileField profileId={data.user?.profile_id} />
                            <input
                              type="hidden"
                              name="id"
                              value={source.id}
                            /><button
                              type="submit"
                              class={`relative h-7 w-12 rounded-full p-1 transition ${sourceEnabled(source) ? "preset-filled-primary-500" : "preset-filled-surface-500"}`}
                              role="switch"
                              aria-label={`Toggle ${source.label}`}
                              aria-checked={sourceEnabled(source)}
                              disabled={isPending(`toggle-source-${source.id}`)}
                              aria-busy={isPending(`toggle-source-${source.id}`)}
                              ><span
                                class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition ${sourceEnabled(source) ? "translate-x-5" : ""}`}
                              ></span></button
                            >
                          </form>
                        </div>{/each}
                    {:else}
                      <div class="border-b border-surface-200-800 px-1 py-5 text-sm text-surface-600-400">
                        {group.key === "permanent"
                          ? "No permanent feeds yet. Add a source to scan it on your cadence."
                          : "No one-time imports yet. Add a source when you want a single snapshot."}
                      </div>
                    {/if}
                  {/each}
                    </div>
                  {/key}
                </div>
              </div>
              <div class="min-w-0 space-y-5">
                <form
                  in:fly={{
                    y: 12,
                    duration: motionDuration(300),
                    delay: motionDelay(1),
                  }}
                  class="card min-w-0 preset-tonal-surface p-4 sm:p-6"
                  method="POST"
                  action="?/configureSourceSchedule"
                  use:enhance={setPending("source-schedule", optimisticSourceSchedule)}
                >
                  <ProfileField profileId={data.user?.profile_id} />
                  <div class="mb-4 flex items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"
                      ><RefreshCw size={18} /></span
                    >
                    <div>
                      <h2 class="font-semibold text-surface-950-50">
                        Refresh cadence
                      </h2>
                      <p class="text-sm text-surface-700-300">
                        Permanent feeds only
                      </p>
                    </div>
                  </div>
                  <label class="block text-sm font-medium text-surface-800-200"
                    >Scan permanent sources<select
                      class="select mt-2 w-full min-w-0 max-w-full"
                      name="intervalHours"
                    >
                      <option value="0" selected={sourceSyncIntervalHours === 0}>Manual only</option>
                      <option value="6" selected={sourceSyncIntervalHours === 6}>Every 6 hours</option>
                      <option value="24" selected={sourceSyncIntervalHours === 24}>Daily (UTC)</option>
                      <option value="168" selected={sourceSyncIntervalHours === 168}>Weekly</option>
                    </select></label
                  >
                  <p class="mt-3 text-sm leading-6 text-surface-700-300">
                    One-time imports never enter this loop. A scheduled pass
                    keeps scanning even when the browser is closed.
                  </p>
                  <button
                    class="btn mt-4 min-h-11 w-full preset-filled-secondary-500"
                    type="submit"
                    disabled={isPending("source-schedule")}
                    aria-busy={isPending("source-schedule")}
                    >{#if isPending("source-schedule")}<RefreshCw
                        size={16}
                        class="animate-spin"
                      />{:else}<Check size={16} />{/if} Save cadence</button
                  >
                </form>
                <form
                  in:fly={{
                    y: 12,
                    duration: motionDuration(300),
                    delay: motionDelay(2),
                  }}
                  class="card h-fit min-w-0 preset-tonal-surface p-4 sm:p-6"
                  method="POST"
                  action="?/source"
                  use:enhance={setPending("source")}
                >
                  <ProfileField profileId={data.user?.profile_id} />
                  <div class="flex items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"
                      ><Link2 size={18} /></span
                    >
                    <h2 class="text-lg font-semibold text-surface-950-50">
                      Add a source
                    </h2>
                  </div>
                  <label
                    class="mt-5 block text-sm font-medium text-surface-800-200"
                    >Name (optional)<input
                      class="input mt-2 w-full min-w-0 max-w-full"
                      name="label"
                      placeholder="Defaults to the website name"
                    /></label
                  ><label
                    class="mt-4 block text-sm font-medium text-surface-800-200"
                    >Public URL<input
                      class="input mt-2 w-full min-w-0 max-w-full"
                      name="url"
                      type="url"
                      placeholder="https://…"
                      required
                    /></label
                  ><label
                    class="mt-4 block text-sm font-medium text-surface-800-200"
                    >Source type<select class="select mt-2 w-full min-w-0 max-w-full" name="lifecycle">
                      <option value="permanent">Permanent feed · keep it fresh</option>
                      <option value="one_time">One-time import · scan once</option>
                    </select></label
                  ><details class="mt-4 border-l-2 border-tertiary-500/30 pl-3">
                    <summary class="min-h-8 cursor-pointer py-2 text-sm font-medium text-tertiary-600-400 hover:text-tertiary-500-300">
                      Optional filters
                    </summary>
                    <div class="mt-3 space-y-3">
                      <label class="block text-xs font-medium text-surface-800-200">
                        Include genres
                        <input
                          class="input mt-1 w-full min-w-0 max-w-full text-sm"
                          name="includeGenres"
                          placeholder="science fiction, fantasy"
                        />
                      </label>
                      <label class="block text-xs font-medium text-surface-800-200">
                        Exclude genres
                        <input
                          class="input mt-1 w-full min-w-0 max-w-full text-sm"
                          name="excludeGenres"
                          placeholder="romance, cookbook"
                        />
                      </label>
                      <p class="text-xs leading-5 text-surface-600-400">
                        Comma-separated. An include filter requires a matching genre tag; untagged books remain eligible with exclusions alone.
                      </p>
                    </div>
                  </details
                  ><button
                    class="btn mt-5 min-h-11 w-full preset-filled-tertiary-500"
                    type="submit"
                    disabled={isPending("source")}
                    aria-busy={isPending("source")}
                    >{#if isPending("source")}<RefreshCw
                        size={16}
                        class="animate-spin"
                      />{:else}<Link2 size={16} />{/if} Add source</button
                  >
                </form>
              </div>
            </section>
          </div>
        {:else}
          <div
            data-view="settings"
            class="col-start-1 row-start-1"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            <section class="mb-6 max-w-2xl">
              <h1
                class="text-4xl font-semibold tracking-tight text-surface-950-50 sm:text-5xl"
              >
                Settings.
              </h1>
            </section>
            <section class="grid gap-5 lg:grid-cols-2">
              <section class="card preset-tonal-surface p-5 sm:p-6 lg:col-span-2">
                <div class="mb-5 flex items-start gap-3">
                  <span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><Database size={19} /></span>
                  <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-3">
                      <h2 class="text-lg font-semibold text-surface-950-50">Backup &amp; recovery</h2>
                      <span class={`badge ${data.backupStatus.enabled ? "preset-tonal-success" : "preset-tonal-surface"}`}>
                        {data.backupStatus.enabled ? `Every ${data.backupStatus.interval_hours} hours` : "Scheduled backups off"}
                      </span>
                    </div>
                    <p class="mt-1 text-sm leading-6 text-surface-700-300">
                      Last successful backup: <strong class="font-medium text-surface-950-50">{formatBackupTime(data.backupStatus.last_success_at)}</strong>.
                      {#if data.backupStatus.enabled} Bookward keeps the latest {data.backupStatus.retention_count} snapshots in its private backup volume.{/if}
                    </p>
                  </div>
                </div>

                {#if data.backupStatus.last_error}
                  <p class="mb-4 border-l-2 border-warning-500 preset-tonal-warning p-3 text-sm" role="status">{data.backupStatus.last_error}</p>
                {/if}

                <p class="mb-4 border-l-2 border-warning-500/50 preset-tonal-warning p-3 text-sm leading-6 text-surface-800-200">
                  Backups contain private reading data, encrypted integration credentials, and a matching generated-key recovery file. Both files stay in the engine's private backup directory and are restored together. A configured <code>AFTERWORD_SECRET_KEY</code> remains external and must stay the same.
                </p>

                <div class="grid gap-5 lg:grid-cols-2">
                  <div>
                    <form method="POST" action="?/createBackup" use:enhance={setPending("create-backup")}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <button class="btn min-h-11 w-full preset-filled-secondary-500" type="submit" disabled={isPending("create-backup")} aria-busy={isPending("create-backup")}>
                        {#if isPending("create-backup")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Create backup now
                      </button>
                    </form>
                    <p class="mt-3 text-xs leading-5 text-surface-600-400">The snapshots and key recovery files are stored on the server and can be restored here.</p>
                    <div class="mt-4 space-y-2">
                      {#if data.backupStatus.backups.length}
                        {#each data.backupStatus.backups as backup (backup.id)}
                          <div class="flex flex-wrap items-center justify-between gap-3 border-b border-surface-200-800 py-2 text-sm last:border-0">
                            <span class="min-w-0 text-surface-800-200">{formatBackupTime(backup.created_at)} <span class="text-surface-600-400">· {formatBackupSize(backup.size_bytes)}</span></span>
                          </div>
                        {/each}
                      {:else}
                        <p class="py-2 text-sm text-surface-600-400">No backup files are available yet.</p>
                      {/if}
                    </div>
                  </div>

                  <div class="border border-surface-300-700/50 p-4">
                    <h3 class="font-semibold text-surface-950-50">Restore a backup</h3>
                    <p class="mt-1 text-sm leading-6 text-surface-700-300">Restoring replaces the current database. Bookward first creates a safety backup of the current state.</p>
                    {#if data.backupStatus.backups.length}
                      <form class="mt-4 space-y-3" method="POST" action="?/restoreBackup" use:enhance={setPending("restore-backup")}>
                        <ProfileField profileId={data.user?.profile_id} />
                        <label class="block text-sm font-medium text-surface-800-200">Backup<select class="select mt-2 w-full" name="backupId" required>
                          {#each data.backupStatus.backups as backup (backup.id)}
                            <option value={backup.id}>{formatBackupTime(backup.created_at)} · {formatBackupSize(backup.size_bytes)}</option>
                          {/each}
                        </select></label>
                        <label class="block text-sm font-medium text-surface-800-200">Type RESTORE to confirm<input class="input mt-2 w-full" name="confirm" autocomplete="off" required pattern="RESTORE" /></label>
                        <button class="btn min-h-11 w-full preset-filled-warning-500" type="submit" disabled={isPending("restore-backup")} aria-busy={isPending("restore-backup")}>
                          {#if isPending("restore-backup")}<RefreshCw size={16} class="animate-spin" />{:else}<RefreshCw size={16} />{/if} Restore selected backup
                        </button>
                      </form>
                    {:else}
                      <p class="mt-4 text-sm text-surface-600-400">Create a backup before using restore.</p>
                    {/if}
                  </div>
                </div>
              </section>
              <form
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(0),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
                method="POST"
                action="?/connectGoodreads"
                use:enhance={setPending("goodreads-rss")}
              >
                <ProfileField profileId={data.user?.profile_id} />
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"
                      ><BookOpen size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      Goodreads RSS
                    </h2>
                  </div>
                  <span class="badge shrink-0 preset-tonal-success"
                    >Incremental</span
                  >
                </div>
                <p class="mt-1 text-sm leading-6 text-surface-700-300">
                  Pull recent reads from a public read-shelf feed. The saved
                  link syncs automatically each night at 02:00 UTC.
                </p>
                {#if data.profile.goodreads_rss_last_sync_at}
                  <p class="mt-2 text-xs text-surface-600-400">
                    Last successful sync (UTC): {data.profile.goodreads_rss_last_sync_at}
                  </p>
                {/if}
                {#if data.profile.goodreads_rss_last_status === "failed"}
                  <p class="mt-3 border-l-2 border-error-500 preset-tonal-error p-3 text-sm leading-6" role="alert">
                    Goodreads RSS sync failed{#if data.profile.goodreads_rss_last_attempt_at} on <time datetime={data.profile.goodreads_rss_last_attempt_at}>{data.profile.goodreads_rss_last_attempt_at}</time> UTC{/if}.
                    {data.profile.goodreads_rss_last_error || "Try syncing the feed again."}
                  </p>
                {/if}
                <label
                  class="mt-5 block text-sm font-medium text-surface-800-200"
                  >Public shelf RSS URL<input
                    class="input mt-2"
                    name="url"
                    type="url"
                    value={data.profile.goodreads_url}
                    placeholder="https://www.goodreads.com/review/list_rss/…"
                    required
                  /></label
                ><button
                  class="btn mt-5 min-h-11 w-full preset-filled-secondary-500"
                  type="submit"
                  disabled={isPending("goodreads-rss")}
                  aria-busy={isPending("goodreads-rss")}
                  >{#if isPending("goodreads-rss")}<RefreshCw
                      size={16}
                      class="animate-spin"
                    />{:else}<RefreshCw size={16} />{/if} Save &amp; sync now</button
                >
                {#if data.profile.goodreads_url}
                  <button
                    class="btn mt-2 min-h-10 w-full preset-tonal-surface"
                    type="submit"
                    formaction="?/disconnectGoodreads"
                    formnovalidate
                    disabled={isPending("goodreads-rss")}
                  >Remove saved feed</button>
                {/if}
              </form>
              <form
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(1),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
                method="POST"
                action="?/importCsv"
                enctype="multipart/form-data"
                use:enhance={setPending("csv-import")}
              >
                <ProfileField profileId={data.user?.profile_id} />
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"
                      ><Upload size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      CSV library export
                    </h2>
                  </div>
                  <span class="badge shrink-0 preset-tonal-surface"
                    >Read-shelf history</span
                  >
                </div>
                <p class="mt-1 text-sm leading-6 text-surface-700-300">
                  Import completed books, ratings, and read dates from a Goodreads or The StoryGraph CSV export. Bookward detects the format automatically.
                </p>
                <label
                  class="mt-5 block text-sm font-medium text-surface-800-200"
                  >Goodreads or StoryGraph CSV<input
                    class="input mt-2"
                    name="file"
                    type="file"
                    accept=".csv,text/csv"
                    required
                  /></label
                ><button
                  class="btn mt-5 min-h-11 w-full preset-filled-tertiary-500"
                  type="submit"
                  disabled={isPending("csv-import")}
                  aria-busy={isPending("csv-import")}
                  >{#if isPending("csv-import")}<RefreshCw
                      size={16}
                      class="animate-spin"
                    />{:else}<Upload size={16} />{/if} Import CSV</button
                >
              </form>
              <form
                id="librarr-settings"
                tabindex="-1"
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(2),
                }}
                class="card scroll-mt-4 preset-tonal-surface p-5 sm:p-6"
                method="POST"
                action="?/configureLibrar"
                use:enhance={setPending("librarr", optimisticLibrarr)}
              >
                <ProfileField profileId={data.user?.profile_id} />
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"
                      ><Library size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      Librarr
                    </h2>
                  </div>
                  <span
                    class={`badge shrink-0 ${librarrConnected ? "preset-tonal-success" : "preset-tonal-surface"}`}
                    >{librarrConnected
                      ? "Configured"
                      : "Not configured"}</span
                  >
                </div>
                <p class="mt-1 text-sm leading-6 text-surface-700-300">
                  Choose a default format for the waitlist, or search Librarr
                  and add a result immediately.
                </p>
                <label
                  class="mt-5 block text-sm font-medium text-surface-800-200"
                  >Server URL<input
                    class="input mt-2"
                    name="url"
                    type="url"
                    value={librarrUrl}
                    placeholder="http://librarr:5050"
                    required
                  /></label
                ><label
                  class="mt-4 block text-sm font-medium text-surface-800-200"
                  >API key<input
                    class="input mt-2"
                    name="apiKey"
                    type="password"
                    placeholder={librarrConnected
                      ? "Leave blank to keep current key"
                      : "Your Librarr API key"}
                  /></label
                ><label
                  class="mt-4 block text-sm font-medium text-surface-800-200"
                  >Default waitlist format<select
                    class="select mt-2"
                    name="mediaType"
                    ><option
                      value="audiobook"
                      selected={configuredLibrarrMediaType === "audiobook"}
                      >Audiobook</option
                    ><option
                      value="ebook"
                      selected={configuredLibrarrMediaType === "ebook"}
                      >Ebook</option
                    ></select
                  ></label
                ><button
                  class="btn mt-5 min-h-11 w-full preset-filled-secondary-500"
                  type="submit"
                  disabled={isPending("librarr")}
                  aria-busy={isPending("librarr")}
                  >{#if isPending("librarr")}<RefreshCw
                      size={16}
                      class="animate-spin"
                    />{:else}<Library size={16} />{/if} Save Librarr connection</button
                >
              </form>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(3),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
              >
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"
                      ><Settings2 size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      Embedding provider
                    </h2>
                  </div>
                  <span class="badge shrink-0 preset-tonal-surface"
                    >{embeddingBackend}</span
                  >
                </div>
                <form
                  id="embedding-settings"
                  class="space-y-4"
                  method="POST"
                  action="?/configureEmbeddings"
                  use:enhance={setPending("embeddings", optimisticEmbeddings)}
                >
                  <ProfileField profileId={data.user?.profile_id} />
                  <label class="block text-sm font-medium text-surface-800-200"
                    >Provider<select class="select mt-2" name="backend"
                      ><option
                        value="local"
                        selected={embeddingBackend === "local"}
                        >Local CPU · zero download</option
                      ><option
                        value="fastembed"
                        selected={embeddingBackend ===
                          "fastembed"}>Local BGE · FastEmbed</option
                      ><option
                        value="ollama"
                        selected={embeddingBackend === "ollama"}
                        >Ollama / Qwen</option
                      ><option
                        value="openai-compatible"
                        selected={embeddingBackend ===
                          "openai-compatible"}>OpenAI-compatible API</option
                      ></select
                    ></label
                  ><label class="block text-sm font-medium text-surface-800-200"
                    >Model<input
                      class="input mt-2"
                      name="model"
                      value={embeddingModel}
                      placeholder="qwen3-embedding:0.6b"
                      required
                    /></label
                  ><label class="block text-sm font-medium text-surface-800-200"
                    >Endpoint<input
                      class="input mt-2"
                      name="url"
                      type="url"
                      value={embeddingUrl}
                      placeholder="http://ollama:11434"
                    /></label
                  ><label class="block text-sm font-medium text-surface-800-200"
                    >API key<input
                      class="input mt-2"
                      name="apiKey"
                      type="password"
                      placeholder={data.profile.embedding_api_key_set
                        ? "Already configured"
                        : "Optional"}
                    /></label
                  ><button
                    class="btn min-h-11 w-full preset-filled-primary-500"
                    type="submit"
                    disabled={isPending("embeddings")}
                    aria-busy={isPending("embeddings")}
                    >{#if isPending("embeddings")}<RefreshCw
                        size={16}
                        class="animate-spin"
                      />{:else}<Check size={16} />{/if} Save and test provider</button
                  >
                </form>
                <div
                  in:fly={{ y: 8, duration: motionDuration(220) }}
                  class="mt-6 flex flex-col gap-3 border border-warning-500/30 preset-tonal-warning p-4"
                  role="note"
                >
                  <div class="flex w-full items-start gap-3">
                    <TriangleAlert
                      size={22}
                      class="mt-0.5 shrink-0 text-warning-600-400"
                    />
                    <div class="min-w-0 flex-1">
                      <p class="text-base font-semibold text-surface-950-50">
                        Changing providers usually requires a full rebuild
                      </p>
                      <p class="mt-1 text-sm leading-6 text-surface-800-200">
                        Embeddings are not compatible between models. Rebuild
                        the entire database after changing providers or models
                        before scoring new books.
                      </p>
                    </div>
                  </div>
                  <form
                    method="POST"
                    action="?/rebuildEmbeddings"
                    use:enhance={setPending("rebuild-embeddings")}
                    class="w-full"
                  >
                    <ProfileField profileId={data.user?.profile_id} />
                    <button
                      class="btn min-h-11 w-full preset-filled-warning-500"
                      type="submit"
                      disabled={isPending("rebuild-embeddings")}
                      aria-busy={isPending("rebuild-embeddings")}
                      >{#if isPending("rebuild-embeddings")}<RefreshCw
                          size={16}
                          class="animate-spin"
                        />{:else}<RefreshCw size={16} />{/if} Rebuild embeddings</button
                    >
                  </form>
                </div>
              </section>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(4),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
              >
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"
                      ><BookOpen size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      Google Books API
                    </h2>
                  </div>
                  <span
                    class={`badge shrink-0 ${data.profile.google_books_api_key_set ? "preset-tonal-success" : "preset-tonal-surface"}`}
                    >{data.profile.google_books_api_key_set
                      ? "Configured"
                      : "Optional"}</span
                  >
                </div>
                <p class="text-sm leading-6 text-surface-700-300">
                  Add a Google Books API key to prevent rate-limit throttling when verifying and enriching new book candidates.
                  <a
                    class="inline-flex min-h-6 items-center gap-1 font-medium text-primary-600-400 underline underline-offset-2"
                    href="https://developers.google.com/books/docs/v1/using"
                    target="_blank"
                    rel="noopener noreferrer"
                    >How to create a key <ExternalLink size={13} /></a
                  >
                </p>
                <form
                  class="mt-4 space-y-4"
                  method="POST"
                  action="?/configureGoogleBooks"
                  use:enhance={setPending("google-books")}
                >
                  <ProfileField profileId={data.user?.profile_id} />
                  <label class="block text-sm font-medium text-surface-800-200"
                    >API key<input
                      class="input mt-2"
                      name="apiKey"
                      type="password"
                      placeholder={data.profile.google_books_api_key_set
                        ? "Already configured · leave blank to keep"
                        : "AIza…"}
                    /></label
                  >
                  {#if data.profile.google_books_api_key_set}
                    <label
                      class="flex min-h-10 cursor-pointer items-center gap-3 text-xs text-surface-700-300"
                      ><input class="checkbox size-6 shrink-0" type="checkbox" name="clear" />
                      Clear saved API key</label
                    >
                  {/if}
                  <button
                    class="btn min-h-11 w-full preset-filled-primary-500"
                    type="submit"
                    disabled={isPending("google-books")}
                    aria-busy={isPending("google-books")}
                    >{#if isPending("google-books")}<RefreshCw
                        size={16}
                        class="animate-spin"
                      />{:else}<Check size={16} />{/if} Save Google Books key</button
                  >
                </form>
              </section>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(4),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
              >
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-primary"
                      ><Sparkles size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      LLM reranker
                    </h2>
                  </div>
                  <span
                    class={`badge shrink-0 ${llmEnabled ? "preset-tonal-success" : "preset-tonal-surface"}`}
                    >{llmEnabled ? "Enabled" : "Disabled"}</span
                  >
                </div>
                <p class="text-sm leading-6 text-surface-700-300">
                  Optionally rerank top recommendations with an OpenAI-compatible LLM. Pick a provider and model from
                  <a
                    class="inline-flex min-h-6 items-center font-medium text-primary-600-400 underline underline-offset-2"
                    href="https://models.dev"
                    target="_blank"
                    rel="noopener noreferrer"
                    >models.dev</a
                  >
                  or enter an endpoint directly.
                </p>
                <div class="mt-4 space-y-3 border border-surface-300-700/50 p-3">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <span class="text-xs font-semibold uppercase tracking-wide text-surface-600-400"
                      >models.dev selector</span
                    >
                    <button
                      type="button"
                      class="btn btn-sm min-h-9 preset-tonal-secondary"
                      onclick={() => void loadLlmCatalog()}
                      disabled={llmCatalogLoading}
                    >
                      {#if llmCatalogLoading}<RefreshCw
                          size={14}
                          class="animate-spin"
                        />{:else}<Search size={14} />{/if}
                      {llmCatalogProviders.length ? "Refresh catalog" : "Load models.dev catalog"}
                    </button>
                  </div>
                  {#if llmCatalogError}
                    <p class="text-xs text-error-600-400" role="alert">{llmCatalogError}</p>
                  {/if}
                  {#if llmCatalogProviders.length > 0}
                    <div class="grid gap-3 sm:grid-cols-2">
                      <div>
                        <label class="block text-xs font-medium text-surface-700-300"
                          >Filter provider<input
                            class="input mt-1"
                            type="search"
                            bind:value={llmProviderSearch}
                            placeholder="Search OpenAI, Google, Groq…"
                          /></label
                        >
                        <label class="mt-2 block text-xs font-medium text-surface-700-300"
                          >Provider<select
                            class="select mt-1"
                            value={llmProvider}
                            onchange={(event) =>
                              handleLlmProviderChange(
                                (event.currentTarget as HTMLSelectElement).value,
                              )}
                          >
                            <option value="">Choose provider…</option>
                            {#each filteredLlmProviders as provider (provider.id)}
                              <option value={provider.id}
                                >{provider.name} ({provider.models.length})</option
                              >
                            {/each}
                          </select></label
                        >
                      </div>
                      <div>
                        <label class="block text-xs font-medium text-surface-700-300"
                          >Filter model<input
                            class="input mt-1"
                            type="search"
                            bind:value={llmModelSearch}
                            placeholder="Search model ID or name…"
                            disabled={!selectedCatalogProvider}
                          /></label
                        >
                        <label class="mt-2 block text-xs font-medium text-surface-700-300"
                          >Model<select
                            class="select mt-1"
                            value={llmModel}
                            disabled={!selectedCatalogProvider}
                            onchange={(event) => {
                              llmModelOverride = (
                                event.currentTarget as HTMLSelectElement
                              ).value;
                            }}
                          >
                            <option value="">Choose model…</option>
                            {#each filteredLlmModels as model (model.id)}
                              <option value={model.id}
                                >{model.name} ({model.id})</option
                              >
                            {/each}
                          </select></label
                        >
                      </div>
                    </div>
                  {/if}
                </div>
                <form
                  class="mt-4 space-y-4"
                  method="POST"
                  action="?/configureLlmRerank"
                  use:enhance={setPending("llm-rerank")}
                >
                  <ProfileField profileId={data.user?.profile_id} />
                  <input type="hidden" name="provider" value={llmProvider} />
                  <label
                    class="flex min-h-11 cursor-pointer items-center gap-3 text-sm font-medium text-surface-800-200"
                    ><input
                      class="checkbox size-6 shrink-0"
                      type="checkbox"
                      name="enabled"
                      checked={llmEnabled}
                      onchange={(event) =>
                        (llmEnabledOverride = (
                          event.currentTarget as HTMLInputElement
                        ).checked)}
                    />
                    Enable LLM reranking</label
                  >
                  <label class="block text-sm font-medium text-surface-800-200"
                    >API URL<input
                      class="input mt-2"
                      name="apiUrl"
                      type="url"
                      value={llmApiUrl}
                      oninput={(event) =>
                        (llmApiUrlOverride = (
                          event.currentTarget as HTMLInputElement
                        ).value)}
                      placeholder="https://api.openai.com/v1/chat/completions"
                    /></label
                  >
                  <label class="block text-sm font-medium text-surface-800-200"
                    >Model<input
                      class="input mt-2"
                      name="model"
                      value={llmModel}
                      oninput={(event) =>
                        (llmModelOverride = (
                          event.currentTarget as HTMLInputElement
                        ).value)}
                      placeholder="gpt-4o-mini"
                    /></label
                  >
                  <label class="block text-sm font-medium text-surface-800-200"
                    >API key<input
                      class="input mt-2"
                      name="apiKey"
                      type="password"
                      placeholder={llmRerank.api_key_set
                        ? "Already configured · leave blank to keep"
                        : "sk-…"}
                    /></label
                  >
                  {#if llmRerank.api_key_set}
                    <label
                      class="flex min-h-10 cursor-pointer items-center gap-3 text-xs text-surface-700-300"
                      ><input class="checkbox size-6 shrink-0" type="checkbox" name="clearApiKey" />
                      Clear saved LLM API key</label
                    >
                  {/if}
                  <button
                    class="btn min-h-11 w-full preset-filled-primary-500"
                    type="submit"
                    disabled={isPending("llm-rerank")}
                    aria-busy={isPending("llm-rerank")}
                    >{#if isPending("llm-rerank")}<RefreshCw
                        size={16}
                        class="animate-spin"
                      />{:else}<Check size={16} />{/if} Save LLM reranker</button
                  >
                </form>
              </section>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(4),
                }}
                class="card preset-tonal-surface p-5 sm:p-6 lg:col-span-2"
              >
                <div class="mb-5 flex items-start gap-3">
                  <span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"
                    ><KeyRound size={19} /></span
                  >
                  <div class="min-w-0 flex-1">
                    <h2 class="text-lg font-semibold text-surface-950-50">API access</h2>
                    <p class="mt-1 text-sm leading-6 text-surface-700-300">
                      Connect automations and other apps to Bookward with a token. Tokens are shown only once when created.
                    </p>
                  </div>
                </div>

                <div class="mb-5 border border-surface-300-700/50 p-3 text-sm">
                  <span class="block text-xs font-semibold uppercase tracking-wide text-surface-600-400">API base URL</span>
                  <code class="mt-1 block break-all text-surface-950-50">{page.url.origin}/api/v1</code>
                  <span class="mt-2 block text-xs leading-5 text-surface-700-300">Send the token as <code>Authorization: Bearer &lt;token&gt;</code>.</span>
                </div>

                {#if revealedApiToken}
                  <div class="mb-5 border-l-2 border-success-500 preset-tonal-success p-4" role="alert">
                    <div class="flex items-start gap-3">
                      <Check size={18} class="mt-0.5 shrink-0" />
                      <div class="min-w-0 flex-1">
                        <p class="font-semibold">Copy this token now</p>
                        <p class="mt-1 text-xs leading-5">For your security, Bookward will not display it again.</p>
                        <code class="mt-3 block break-all rounded bg-surface-950-50/10 p-2 text-xs">{revealedApiToken}</code>
                      </div>
                      <button
                        class="btn btn-sm min-h-9 shrink-0 preset-tonal-success"
                        type="button"
                        title="Copy API token"
                        aria-label="Copy API token"
                        onclick={() => void copyApiToken()}
                      ><Copy size={14} /> Copy</button>
                    </div>
                    {#if apiTokenCopyMessage}
                      <p class="mt-3 text-xs" role="status">{apiTokenCopyMessage}</p>
                    {/if}
                  </div>
                {/if}

                <form method="POST" action="?/createApiToken" use:enhance={setPending("create-api-token")} class="flex flex-col gap-3 sm:flex-row sm:items-end">
                  <ProfileField profileId={data.user?.profile_id} />
                  <label class="block min-w-0 flex-1 text-sm font-medium text-surface-800-200">
                    Token name
                    <input class="input mt-2" name="name" maxlength="100" placeholder="Home Assistant" required />
                  </label>
                  <button class="btn min-h-11 preset-filled-primary-500" type="submit" disabled={isPending("create-api-token")} aria-busy={isPending("create-api-token")}>
                    {#if isPending("create-api-token")}<RefreshCw size={16} class="animate-spin" />{:else}<KeyRound size={16} />{/if} Generate token
                  </button>
                </form>

                {#if apiTokens.length}
                  <div class="mt-6 border-t border-surface-300-700/40 pt-5">
                    <h3 class="text-sm font-semibold text-surface-950-50">Existing tokens</h3>
                    <ul class="mt-3 divide-y divide-surface-300-700/40 border border-surface-300-700/40">
                      {#each apiTokens as token (token.id)}
                        <li class="flex flex-wrap items-center gap-3 p-3">
                          <div class="min-w-0 flex-1">
                            <p class="truncate text-sm font-medium text-surface-950-50">{token.name}</p>
                            <p class="mt-1 text-xs text-surface-700-300"><code>{token.token_prefix}…</code> · Created {token.created_at}</p>
                            {#if token.last_used_at}<p class="mt-1 text-xs text-surface-700-300">Last used {token.last_used_at}</p>{/if}
                          </div>
                          {#if token.revoked_at}
                            <span class="badge preset-tonal-error">Revoked</span>
                          {:else}
                            <details>
                              <summary class="btn btn-sm min-h-9 cursor-pointer preset-tonal-error" aria-label={`Revoke API token ${token.name}`}><Trash2 size={14} /> Revoke</summary>
                              <form method="POST" action="?/revokeApiToken" use:enhance={setPending(`revoke-api-token-${token.id}`, optimisticRevokeToken)} class="mt-2 grid max-w-xs justify-items-start gap-2">
                                <ProfileField profileId={data.user?.profile_id} />
                                <input type="hidden" name="id" value={token.id} />
                                <p class="text-xs text-surface-700-300">Apps using this token will lose access to Bookward.</p>
                                <button class="btn btn-sm min-h-9 preset-filled-error-500" type="submit" aria-label={`Confirm revoke: ${token.name}`} disabled={isPending(`revoke-api-token-${token.id}`)} aria-busy={isPending(`revoke-api-token-${token.id}`)}>Confirm revoke</button>
                              </form>
                            </details>
                          {/if}
                        </li>
                      {/each}
                    </ul>
                  </div>
                {/if}
              </section>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(4),
                }}
                class="card preset-tonal-surface p-5 sm:p-6 lg:col-span-2"
              >
                <form method="POST" action="?/configureDigest" use:enhance={setPending("digest-settings", optimisticDigestSettings)}>
                  <ProfileField profileId={data.user?.profile_id} />
                  <div class="flex flex-wrap items-center gap-4">
                    <span class="grid size-11 shrink-0 place-items-center preset-tonal-secondary"><Bell size={20} /></span>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-3">
                        <h2 class="text-lg font-semibold text-surface-950-50">Weekly digest</h2>
                        <span class="badge preset-tonal-surface">{digestEnabled ? "On" : "Off"}</span>
                      </div>
                      <p class="mt-1 text-sm text-surface-700-300">A short list of your strongest new matches, on your schedule.</p>
                    </div>
                    <SwitchControl
                      id="digest-enabled"
                      name="enabled"
                      checked={digestEnabled}
                      ariaLabel="Enable weekly digest"
                      onToggle={(next) => (digestEnabledOverride = next)}
                    />
                  </div>

                  {#if digestEnabled}<div in:slide={{ duration: motionDuration(240) }} out:fade={{ duration: motionDuration(160) }} class="mt-6">
                    <div class="grid gap-5 lg:grid-cols-[0.95fr_1.05fr]">
                      <section class="preset-tonal-surface p-4 sm:p-5">
                        <div class="mb-4 flex items-center justify-between gap-3">
                          <div>
                            <h3 class="font-semibold text-surface-950-50">Schedule</h3>
                            <p class="mt-1 text-sm text-surface-700-300">When should Bookward check in?</p>
                          </div>
                          <span class="badge preset-tonal-secondary">Automatic</span>
                        </div>
                        <div class="grid gap-4 sm:grid-cols-2">
                          <label class="block text-sm font-medium text-surface-800-200">Day<select class="select mt-2" name="day">
                            <option value="1" selected={digestSettings.day === 1}>Monday</option>
                            <option value="2" selected={digestSettings.day === 2}>Tuesday</option>
                            <option value="3" selected={digestSettings.day === 3}>Wednesday</option>
                            <option value="4" selected={digestSettings.day === 4}>Thursday</option>
                            <option value="5" selected={digestSettings.day === 5}>Friday</option>
                            <option value="6" selected={digestSettings.day === 6}>Saturday</option>
                            <option value="7" selected={digestSettings.day === 7}>Sunday</option>
                          </select></label>
                          <label class="block text-sm font-medium text-surface-800-200">Time<input class="input mt-2" name="time" type="time" value={digestSettings.time} required /></label>
                          <label class="block text-sm font-medium text-surface-800-200 sm:col-span-2">Timezone<input class="input mt-2" name="timezone" value={digestSettings.timezone} placeholder="America/Phoenix" required /></label>
                          <label class="block text-sm font-medium text-surface-800-200">Minimum match<input class="input mt-2" name="minimumScore" type="number" min="0" max="100" step="1" value={digestSettings.minimum_score} required /></label>
                          <label class="block text-sm font-medium text-surface-800-200">Books per digest<input class="input mt-2" name="maximumBooks" type="number" min="1" max="20" step="1" value={digestSettings.maximum_books} required /></label>
                        </div>
                        <div class="mt-4 flex items-center justify-between gap-4 border-t border-surface-300-700/40 pt-4 text-sm font-medium text-surface-800-200">
                          <label for="digest-only-new" class="cursor-pointer">
                            <span class="block text-surface-950-50">Only send new books</span>
                            <span class="mt-1 block text-xs font-normal text-surface-700-300">Skip recommendations already included in a digest.</span>
                          </label>
                          <SwitchControl
                            id="digest-only-new"
                            name="onlyNew"
                            checked={digestOnlyNew}
                            ariaLabel="Only send new books"
                            onToggle={(next) => (digestOnlyNewOverride = next)}
                          />
                        </div>
                      </section>

                      <section class="preset-tonal-surface p-4 sm:p-5">
                        <div class="mb-4">
                          <h3 class="font-semibold text-surface-950-50">Delivery</h3>
                          <p class="mt-1 text-sm text-surface-700-300">Choose one or both places to receive it.</p>
                        </div>
                        <div class="grid gap-3">
                          <div class="relative border border-surface-300-700/50 p-4">
                            <div class="flex items-center gap-3">
                              <label for="digest-discord" class="flex min-w-0 flex-1 cursor-pointer items-center gap-3">
                                <span class="grid size-9 shrink-0 place-items-center preset-tonal-secondary"><MessageCircle size={17} /></span>
                                <span class="min-w-0 flex-1"><span class="block font-medium text-surface-950-50">Discord</span><span class="mt-0.5 block text-xs text-surface-700-300">Post to a webhook</span></span>
                              </label>
                              <SwitchControl
                                id="digest-discord"
                                name="channels"
                                value="discord"
                                checked={digestDiscord}
                                ariaLabel="Send digest to Discord"
                                onToggle={(next) => (digestDiscordOverride = next)}
                              />
                            </div>
                            {#if digestDiscord}<div in:slide={{ duration: motionDuration(200) }} out:fade={{ duration: motionDuration(140) }} class="mt-4 space-y-3">
                              <label class="block text-sm font-medium text-surface-800-200">Webhook URL<input class="input mt-2" name="discordWebhook" type="url" placeholder={digestSettings.discord_webhook_set ? "Already configured · leave blank to keep" : "https://discord.com/api/webhooks/…"} /></label>
                              {#if digestSettings.discord_webhook_set}<label class="flex min-h-10 cursor-pointer items-center gap-3 text-xs text-surface-700-300"><input class="checkbox size-6 shrink-0" type="checkbox" name="clearDiscord" /> Clear saved webhook</label>{/if}
                            </div>{:else}<input type="hidden" name="discordWebhook" value="" />{/if}
                          </div>

                          <div class="relative border border-surface-300-700/50 p-4">
                            <div class="flex items-center gap-3">
                              <label for="digest-email" class="flex min-w-0 flex-1 cursor-pointer items-center gap-3">
                                <span class="grid size-9 shrink-0 place-items-center preset-tonal-tertiary"><Mail size={17} /></span>
                                <span class="min-w-0 flex-1"><span class="block font-medium text-surface-950-50">Email</span><span class="mt-0.5 block text-xs text-surface-700-300">Send through your SMTP server</span></span>
                              </label>
                              <SwitchControl
                                id="digest-email"
                                name="channels"
                                value="email"
                                checked={digestEmail}
                                ariaLabel="Send digest by email"
                                activeClass="preset-filled-tertiary-500"
                                focusClass="focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-tertiary-500"
                                onToggle={(next) => (digestEmailOverride = next)}
                              />
                            </div>
                            {#if digestEmail}<div in:slide={{ duration: motionDuration(200) }} out:fade={{ duration: motionDuration(140) }} class="mt-4 space-y-4">
                              <div class="grid gap-4 sm:grid-cols-2">
                                <label class="block text-sm font-medium text-surface-800-200">Recipient<input class="input mt-2" name="emailTo" type="email" value={digestSettings.email_to} placeholder="you@example.com" /></label>
                                <label class="block text-sm font-medium text-surface-800-200">From address<input class="input mt-2" name="emailFrom" type="email" value={digestSettings.email_from} placeholder="bookward@example.com" /></label>
                                <label class="block text-sm font-medium text-surface-800-200 sm:col-span-2">SMTP host<input class="input mt-2" name="smtpHost" value={digestSettings.smtp_host} placeholder="smtp.example.com" /></label>
                                <label class="block text-sm font-medium text-surface-800-200">Port<input class="input mt-2" name="smtpPort" type="number" min="1" max="65535" value={digestSettings.smtp_port} /></label>
                                <label class="block text-sm font-medium text-surface-800-200">Security<select class="select mt-2" name="smtpSecurity"><option value="starttls" selected={digestSettings.smtp_security === "starttls"}>STARTTLS</option><option value="ssl" selected={digestSettings.smtp_security === "ssl"}>SSL / TLS</option><option value="none" selected={digestSettings.smtp_security === "none"}>None</option></select></label>
                                <label class="block text-sm font-medium text-surface-800-200">Username<input class="input mt-2" name="smtpUsername" value="" placeholder={digestSettings.smtp_username_set ? "Already configured · leave blank" : "Optional"} /></label>
                                <label class="block text-sm font-medium text-surface-800-200">Password<input class="input mt-2" name="smtpPassword" type="password" placeholder={digestSettings.smtp_password_set ? "Already configured · leave blank" : "Optional"} /></label>
                              </div>
                              {#if digestSettings.smtp_username_set || digestSettings.smtp_password_set}<label class="flex min-h-10 cursor-pointer items-center gap-3 text-xs text-surface-700-300"><input class="checkbox size-6 shrink-0" type="checkbox" name="clearSmtpCredentials" /> Clear saved credentials</label>{/if}
                            </div>{:else}<input type="hidden" name="emailTo" value={digestSettings.email_to} /><input type="hidden" name="emailFrom" value={digestSettings.email_from} /><input type="hidden" name="smtpHost" value={digestSettings.smtp_host} /><input type="hidden" name="smtpPort" value={digestSettings.smtp_port} /><input type="hidden" name="smtpSecurity" value={digestSettings.smtp_security} />{/if}
                          </div>
                        </div>
                      </section>
                    </div>
                    <label class="mt-5 block text-sm font-medium text-surface-800-200">Bookward public URL<input class="input mt-2" name="appUrl" type="url" value={digestSettings.app_url} placeholder="https://bookward.example.com" required /></label>
                  </div>
                  {:else}
                    <input type="hidden" name="day" value={digestSettings.day} /><input type="hidden" name="time" value={digestSettings.time} /><input type="hidden" name="timezone" value={digestSettings.timezone} /><input type="hidden" name="minimumScore" value={digestSettings.minimum_score} /><input type="hidden" name="maximumBooks" value={digestSettings.maximum_books} /><input type="hidden" name="appUrl" value={digestSettings.app_url} /><input type="hidden" name="smtpPort" value={digestSettings.smtp_port} /><input type="hidden" name="smtpSecurity" value={digestSettings.smtp_security} />
                    {#if digestOnlyNew}<input type="hidden" name="onlyNew" value="on" />{/if}
                    {#if digestDiscord}<input type="hidden" name="channels" value="discord" />{/if}
                    {#if digestEmail}<input type="hidden" name="channels" value="email" />{/if}
                    <input type="hidden" name="emailTo" value={digestSettings.email_to} /><input type="hidden" name="emailFrom" value={digestSettings.email_from} /><input type="hidden" name="smtpHost" value={digestSettings.smtp_host} />
                  {/if}
                  <div class="mt-6 flex flex-col gap-3 border-t border-surface-300-700/40 pt-5 sm:flex-row sm:items-center sm:justify-between">
                    {#if digestEnabled}<p class="text-sm text-surface-700-300">Bookward checks this schedule even while the browser is closed.</p>{:else}<p in:fade={{ duration: motionDuration(180) }} class="text-sm text-surface-700-300">Turn on the digest to choose a schedule and delivery channel.</p>{/if}
                    <button class="btn min-h-11 w-full preset-filled-secondary-500 sm:w-auto" type="submit" disabled={isPending("digest-settings")} aria-busy={isPending("digest-settings")}>{#if isPending("digest-settings")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Save digest settings</button>
                  </div>
                </form>
                <div class="mt-5 flex flex-wrap items-center gap-2 border-t border-surface-300-700/40 pt-5">
                  {#each ["discord", "email"] as channel}
                    {@const configured = channel === "discord" ? digestDiscord : digestEmail}
                    {#if configured}<form method="POST" action="?/sendDigestTest" use:enhance={setPending(`digest-test-${channel}`)}>
                      <ProfileField profileId={data.user?.profile_id} />
                      <input type="hidden" name="channel" value={channel} />
                      <button class="btn btn-sm min-h-10 preset-tonal-secondary" type="submit" disabled={isPending(`digest-test-${channel}`)} aria-busy={isPending(`digest-test-${channel}`)}>{#if channel === "discord"}<MessageCircle size={15} />{:else}<Mail size={15} />{/if}{#if isPending(`digest-test-${channel}`)}<RefreshCw size={15} class="animate-spin" />{:else}Send {channel} test{/if}</button>
                    </form>{/if}
                  {/each}
                  <form method="POST" action="?/runDigest" use:enhance={setPending("digest-run")} class="ml-auto">
                    <ProfileField profileId={data.user?.profile_id} />
                    <button class="btn btn-sm min-h-10 preset-filled-tertiary-500" type="submit" disabled={!digestEnabled || (!digestDiscord && !digestEmail) || isPending("digest-run")} aria-busy={isPending("digest-run")}><Send size={15} /> Run digest now</button>
                  </form>
                </div>
                {#if digestSettings.last_delivery}
                  {@const delivery = digestSettings.last_delivery}
                  <div class={`mt-4 flex flex-wrap items-center gap-3 border-l-2 p-3 text-sm ${delivery.status === "failed" ? "border-error-500 preset-tonal-error" : delivery.status === "sent" ? "border-success-500 preset-tonal-success" : "border-secondary-500 preset-tonal-secondary"}`} role="status">
                    {#if delivery.status === "sent"}<Check size={16} class="shrink-0" />{:else if delivery.status === "failed"}<TriangleAlert size={16} class="shrink-0" />{:else}<RefreshCw size={16} class="shrink-0 animate-spin" />{/if}
                    <span class="min-w-0 flex-1">Last {delivery.channel} delivery: <strong>{delivery.status}</strong>{#if delivery.sent_at} · {formatDeliveryDate(delivery.sent_at)}{:else if delivery.updated_at} · {formatDeliveryDate(delivery.updated_at)}{/if}{#if delivery.error}<span class="mt-1 block text-xs">{delivery.error}</span>{/if}</span>
                    {#if delivery.status === "failed"}<form method="POST" action="?/retryDigest" use:enhance={setPending("digest-retry")}>
                      <ProfileField profileId={data.user?.profile_id} /><input type="hidden" name="id" value={delivery.id} /><button class="btn btn-sm min-h-9 preset-tonal-secondary" type="submit" disabled={isPending("digest-retry")} aria-busy={isPending("digest-retry")}><RefreshCw size={14} /> Retry</button></form>{/if}
                  </div>
                {/if}
              </section>
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(5),
                }}
                class="card preset-tonal-surface p-5 sm:p-6 lg:col-span-2"
              >
                <div class="mb-5 flex flex-wrap items-center gap-3">
                  <span
                    class="grid size-10 shrink-0 place-items-center preset-tonal-primary"
                    ><BookOpen size={19} /></span
                  >
                  <h2 class="flex-1 text-lg font-semibold text-surface-950-50">Recent reads</h2>
                  <button type="button" class="btn btn-sm min-h-9 preset-tonal-secondary" onclick={() => go("decisions", "read")}>View full history <ArrowRight size={14} /></button>
                </div>
                {#if data.history.length}<div
                    class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3"
                  >
                    {#each data.history as item (item.id)}<div
                        in:fly={{
                          y: 8,
                          duration: motionDuration(220),
                          delay: motionDelay(item.id % 6, 20),
                        }}
                        out:fade={{ duration: motionDuration(100) }}
                        class="flex min-h-16 items-center gap-3 preset-tonal-surface p-3"
                      >
                        <BookOpen size={15} class="shrink-0 text-primary-500" />
                        <div class="min-w-0 flex-1">
                          <span
                            class="block truncate text-sm font-medium text-surface-950-50"
                            >{item.title}</span
                          ><small
                            class="block truncate text-xs text-surface-700-300"
                            >{item.author}</small
                          >
                        </div>
                        {#if item.rating}<StarRatingBadge
                            rating={item.rating}
                            class="shrink-0 text-xs font-semibold text-primary-600-400"
                          />{/if}
                      </div>{/each}
                  </div>{:else}<p
                    in:fade={{ duration: motionDuration(220) }}
                    class="text-sm text-surface-700-300"
                  >
                    Import a Goodreads history to start tuning your
                    recommendations.
                  </p>{/if}
              </section>
            </section>
          </div>
        {/if}
      </div>
    </main>
  </div>

  <nav
    class="fixed inset-x-0 bottom-0 z-40 flex border-t preset-filled-surface-50-950 pb-[env(safe-area-inset-bottom)] md:hidden"
    aria-label="Mobile navigation"
  >
    {#each navItems as item}{@const Icon = item.icon}<button
        type="button"
        class={`relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 text-xs ${activeView === item.id ? "text-primary-600-400" : "text-surface-700-300"}`}
        aria-current={activeView === item.id ? "page" : undefined}
        onclick={() => go(item.id)}
        ><Icon
          size={19}
          strokeWidth={activeView === item.id ? 2.2 : 1.8}
        /><span class="flex items-center gap-1">{item.shortLabel}{#if item.id === "saved"}<span class="badge badge-sm preset-tonal-secondary">{shortlistTotalCount}</span>{/if}</span>{#if activeView === item.id}<span
            in:scale={{ duration: motionDuration(150) }}
            out:fade={{ duration: motionDuration(90) }}
            class="absolute bottom-1 size-1 rounded-full preset-filled-primary-500"
            aria-hidden="true"
          ></span>{/if}</button
      >{/each}
  </nav>
  {#if markReadBook}
    <div
      in:fade={{ duration: motionDuration(240) }}
      out:fade={{ duration: motionDuration(180) }}
      class="fixed inset-0 z-[60] grid place-items-center overflow-y-auto bg-surface-950/75 p-4 backdrop-blur-md"
      role="presentation"
      onclick={(event) => {
        if (event.target === event.currentTarget) closeMarkRead();
      }}
    >
      <div
        use:modalFocus
        role="dialog"
        aria-modal="true"
        aria-labelledby="mark-read-title"
        aria-describedby="mark-read-description"
        tabindex="-1"
        in:scale={{ duration: motionDuration(260), start: 0.94 }}
        out:scale={{ duration: motionDuration(170), start: 0.97 }}
        class="relative isolate max-h-[min(42rem,calc(100dvh-2rem))] w-full max-w-xl overflow-x-hidden overflow-y-auto rounded-[2rem] border border-primary-500/25 preset-filled-surface-50-950 shadow-2xl shadow-surface-950/60"
      >
        <div class="pointer-events-none absolute inset-0 -z-10 overflow-hidden" aria-hidden="true">
          <div class="absolute -right-20 -top-28 size-72 rounded-full bg-primary-500/15 blur-3xl"></div>
          <div class="absolute -bottom-28 -left-20 size-72 rounded-full bg-secondary-500/10 blur-3xl"></div>
        </div>
        <div class="flex items-start justify-between gap-5 border-b border-surface-200-800/70 p-5 sm:p-7">
          <div class="flex min-w-0 items-center gap-4">
            <span class="grid size-12 shrink-0 place-items-center rounded-2xl border border-primary-500/25 bg-primary-500/10 text-primary-600-400 shadow-inner shadow-primary-500/10">
              <BookOpen size={22} />
            </span>
            <div class="min-w-0">
              <p class="text-[0.68rem] font-semibold uppercase tracking-[0.22em] text-primary-600-400">Keep the story</p>
              <h2 id="mark-read-title" class="mt-1 text-2xl font-semibold tracking-tight text-surface-950-50 sm:text-3xl">Mark Read: {markReadBook.title}</h2>
            </div>
          </div>
          <button
            type="button"
            class="btn btn-icon btn-sm min-h-9 min-w-9 shrink-0 rounded-full preset-tonal-surface"
            aria-label="Close Mark Read dialog"
            data-modal-initial-focus
            onclick={closeMarkRead}
          ><X size={17} /></button>
        </div>

        <form method="POST" action="?/markRead" use:enhance={setPending(`shortlist-read-${markReadBook.id}`, optimisticShortlistRead)}>
          <div class="p-5 sm:p-7">
            <ProfileField profileId={data.user?.profile_id} />
            <input type="hidden" name="id" value={markReadBook.id} />
            <input type="hidden" name="rating" value={markReadRating || ""} />

            <div class="flex items-center gap-4 rounded-2xl border border-surface-200-800/80 bg-surface-950/5 p-3 sm:p-4">
              <div class="relative grid h-18 w-12 aspect-[2/3] shrink-0 place-items-center overflow-hidden rounded-xl border border-surface-200-800/80 bg-surface-900/40">
                <BookOpen size={22} class="text-surface-600-400" aria-hidden="true" />
                {#if markReadBook.cover_url}
                  <img
                    src={markReadBook.cover_url}
                    alt=""
                    width="48"
                    height="72"
                    class="absolute inset-0 h-full w-full object-cover"
                    onerror={(event) => ((event.currentTarget as HTMLImageElement).hidden = true)}
                  />
                {/if}
              </div>
              <div class="min-w-0">
                <p class="line-clamp-2 text-lg font-semibold leading-snug text-surface-950-50">{markReadBook.title}</p>
                <p class="mt-1 truncate text-sm text-surface-700-300">{markReadBook.author}</p>
              </div>
              <span class="ml-auto hidden size-9 shrink-0 place-items-center rounded-full bg-secondary-500/10 text-secondary-600-400 sm:grid" aria-hidden="true"><Check size={17} /></span>
            </div>

            <p id="mark-read-description" class="mt-5 max-w-md text-sm leading-6 text-surface-700-300">Add this book to your read history. You can leave a rating now, or keep the moment unrated.</p>

            <div class="mt-6">
              <div class="flex items-baseline justify-between gap-3">
                <p id="mark-read-rating-label" class="text-sm font-semibold text-surface-950-50">How did it stay with you?</p>
                <span class="text-xs text-surface-600-400">Optional</span>
              </div>
              <div class="mt-3 flex flex-wrap items-center gap-2">
                <RadioGroup
                  bind:this={markReadRatingGroup}
                  items={[1, 2, 3, 4, 5]}
                  value={markReadRating > 0 ? markReadRating : null}
                  labelledBy="mark-read-rating-label"
                  columns={1}
                  selectOnMove
                  clearable
                  class="flex flex-wrap items-center gap-2"
                  onSelect={(rating) => (markReadRating = rating)}
                  onClear={() => (markReadRating = 0)}
                >
                  {#snippet item(rating, radio)}
                    <button
                      type="button"
                      role="radio"
                      aria-label={`${rating} ${rating === 1 ? "star" : "stars"}`}
                      aria-checked={radio.checked}
                      tabindex={radio.tabIndex}
                      class={`grid size-11 place-items-center rounded-xl border text-xl transition duration-200 hover:-translate-y-0.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 ${markReadRating >= rating ? "border-amber-400/50 bg-amber-400/15 text-amber-300 shadow-lg shadow-amber-950/20" : "border-surface-200-800/80 bg-surface-950/5 text-surface-600-400 hover:border-amber-400/30 hover:text-amber-300"}`}
                      onclick={radio.select}
                    ><span aria-hidden="true">★</span></button>
                  {/snippet}
                </RadioGroup>
                {#if markReadRating > 0}<button type="button" class="btn btn-sm ml-1 min-h-10 preset-tonal-surface" onclick={() => { markReadRating = 0; markReadRatingGroup?.focusFirst(); }}>Clear rating</button>{/if}
              </div>
            </div>
          </div>

          <div class="flex flex-col-reverse gap-2 border-t border-surface-200-800/70 bg-surface-950/5 p-5 sm:flex-row sm:justify-end sm:p-6">
            <button type="button" class="btn min-h-11 preset-tonal-surface" onclick={closeMarkRead}>Not yet</button>
            <button
              type="submit"
              class="btn min-h-11 preset-filled-primary-500"
              disabled={isPending(`shortlist-read-${markReadBook.id}`)}
              aria-busy={isPending(`shortlist-read-${markReadBook.id}`)}
            >
              {#if isPending(`shortlist-read-${markReadBook.id}`)}<RefreshCw size={16} class="animate-spin" /> Saving…{:else}<Check size={16} /> Mark Read{/if}
            </button>
          </div>
        </form>
      </div>
    </div>
  {/if}
  {#if librarrSearchOpen}
    <div
      in:fade={{ duration: motionDuration(180) }}
      out:fade={{ duration: motionDuration(120) }}
      class="fixed inset-0 z-50 grid place-items-center bg-surface-950/70 p-4 backdrop-blur-sm"
      role="presentation"
      onclick={closeLibrarrSearch}
    >
      <dialog
        use:modalFocus
        open
        in:scale={{ duration: motionDuration(220) }}
        out:scale={{ duration: motionDuration(140) }}
        class="relative m-0 card flex max-h-[min(44rem,calc(100dvh-2rem))] w-full max-w-2xl flex-col justify-self-center overflow-hidden preset-filled-surface-50-950 shadow-2xl shadow-surface-950/30"
        aria-labelledby="librarr-search-title"
        aria-modal="true"
        onclick={(event) => event.stopPropagation()}
      >
        <div
          class="flex shrink-0 items-start justify-between gap-4 border-b border-surface-200-800 p-5 sm:p-6"
        >
          <div class="min-w-0">
            <p class="text-sm font-medium text-primary-600-400">
              Librarr search
            </p>
            <h2
              id="librarr-search-title"
              class="mt-1 truncate text-2xl font-semibold tracking-tight text-surface-950-50"
            >
              Find a book to add
            </h2>
            <p class="mt-1 truncate text-sm text-surface-700-300">
              {librarrSearchBook?.title} · {librarrSearchBook?.author}
            </p>
          </div>
          <button
            type="button"
            class="btn btn-icon btn-sm min-h-9 min-w-9 shrink-0 preset-tonal-surface"
            aria-label="Close Librarr search"
            onclick={closeLibrarrSearch}><X size={17} /></button
          >
        </div>
        <div class="max-h-[calc(100dvh-11rem)] min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
          <form
            class="flex flex-col gap-3 sm:flex-row sm:items-center"
            onsubmit={(event) => {
              event.preventDefault();
              void searchLibrarr();
            }}
          >
            <label
              class="input flex min-h-12 min-w-0 flex-1 cursor-text items-center gap-2 py-0!"
              ><Search size={17} class="shrink-0 text-surface-600-400" /><input
                class="input-ghost min-h-12 min-w-0 flex-1 self-stretch focus:outline-none focus:ring-0"
                data-modal-initial-focus
                bind:value={librarrQuery}
                aria-label="Search Librarr by title or author"
                placeholder="Search title or author"
              /></label
            >
            <label class="sr-only" for="librarr-media-type">Format</label
            ><select
              id="librarr-media-type"
              class="select min-h-12 sm:mx-1 sm:w-40"
              bind:value={librarrMediaType}
              onchange={handleLibrarrMediaTypeChange}
              ><option value="audiobook">Audiobook</option><option value="ebook"
                >Ebook</option
              ></select
            >
            <button
              type="submit"
              class="btn min-h-12 preset-filled-primary-500"
              aria-busy={librarrSearchState === "searching"}
              >{#if librarrSearchState === "searching"}<RefreshCw
                  size={16}
                  class="animate-spin"
                />{:else}<Search size={16} />{/if} Search</button
            >
          </form>
          {#if librarrSearchMessage}<div
              in:fly={{ y: -8, duration: motionDuration(180) }}
              class="mt-4 flex items-center gap-2 preset-tonal-success p-3 text-sm"
              role="status"
            >
              <Check size={16} />
              {librarrSearchMessage}
            </div>{/if}
          {#if librarrSearchError}<div
              in:fly={{ y: -8, duration: motionDuration(180) }}
              class="mt-4 flex flex-wrap items-center justify-between gap-3 preset-tonal-error p-3 text-sm"
              role="alert"
            >
              <div class="flex min-w-0 flex-1 items-start gap-2">
                <CircleHelp size={16} class="mt-0.5 shrink-0" />
                <span>{librarrSearchError}</span>
              </div>
              {#if canRetryLibrarrSearch(librarrSearchState, librarrQuery)}
                <button
                  type="button"
                  class="btn btn-sm min-h-9 shrink-0 preset-tonal-surface"
                  onclick={() => void searchLibrarr()}
                >
                  <RefreshCw size={14} /> Retry
                </button>
              {/if}
            </div>{/if}
          {#if librarrSearchState === "searching" && librarrResults.length === 0}
            <div
              in:fade={{ duration: motionDuration(160) }}
              class="grid min-h-48 place-items-center text-sm text-surface-700-300"
            >
              <RefreshCw size={22} class="animate-spin text-primary-500" /> Searching
              Librarr…
            </div>
          {:else if librarrSearchState === "searching"}
            <div
              class="mt-4 flex items-center gap-2 text-sm text-surface-700-300"
              role="status"
              aria-live="polite"
            >
              <RefreshCw size={15} class="animate-spin text-primary-500" />
              Searching Librarr… {librarrResults.length} matches so far
            </div>
          {/if}
          {#if shouldShowLibrarrEmptyBanner(librarrSearchState, librarrResults.length, librarrSearchError)}
            <div
              in:fade={{ duration: motionDuration(160) }}
              class="grid min-h-48 place-items-center text-center text-sm text-surface-700-300"
            >
              <div>
                <BookOpen size={24} class="mx-auto mb-3 text-primary-500" />
                <p>No {librarrSearchedMediaType} results found.</p>
                <p class="mt-1">Try a title, author, or a shorter search.</p>
              </div>
            </div>
          {/if}
          {#if librarrResults.length}
            {#if highConfidenceLibrarrResults.length > 1}<div class="mt-5 flex flex-wrap items-center justify-between gap-3 border border-primary-500/30 preset-tonal-primary p-3 text-sm"><span><strong>{highConfidenceLibrarrResults.length} high-confidence matches</strong><span class="ml-1 text-surface-700-300">(Librarr score ≥ 90)</span></span><button type="button" class="btn btn-sm min-h-11 preset-filled-primary-500" onclick={() => void addHighConfidenceLibrarrResults()} disabled={librarrAddingKey !== null || librarrBulkAdding} aria-busy={librarrBulkAdding}>{#if librarrBulkAdding}<RefreshCw size={14} class="animate-spin" /> Adding…{:else}<Library size={14} /> Add high-confidence matches{/if}</button></div>{/if}
            <div class="mt-5 space-y-3" aria-live="polite">
              {#each librarrResults as result, index (resultKey(result, index))}
                {@const cover = resultCover(result)}
                <article
                  in:fly={{
                    y: 10,
                    duration: motionDuration(220),
                    delay: motionDelay(index, 24),
                  }}
                  class="flex items-center gap-4 preset-tonal-surface p-3 sm:p-4"
                >
                  <div
                    class="relative grid h-18 w-12 aspect-[2/3] shrink-0 place-items-center overflow-hidden rounded-lg preset-filled-surface-500"
                  >
                    <BookOpen
                      size={22}
                      class="text-surface-600-400"
                      aria-hidden="true"
                    />
                    {#if cover}<img
                        in:fade={{ duration: motionDuration(180) }}
                        class="absolute inset-0 h-full w-full object-cover"
                        src={cover}
                        alt={`Cover of ${resultTitle(result)}`}
                        width="48"
                        height="72"
                        loading="lazy"
                        onerror={(event) =>
                          ((event.currentTarget as HTMLImageElement).hidden =
                            true)}
                      />{/if}
                  </div>
                  <div class="min-w-0 flex-1">
                    <h3
                      class="line-clamp-2 text-base font-semibold text-surface-950-50 sm:text-lg"
                    >
                      {resultTitle(result)}
                    </h3>
                    <p class="mt-1 truncate text-sm text-surface-700-300">
                      {resultAuthor(result)}
                    </p>
                    <span
                      class="mt-1 block text-xs capitalize text-surface-600-400"
                      >{resultFormat(result)}</span
                    >
                    {#if resultScore(result) !== null}<span class="mt-1 block text-xs text-primary-600-400">Librarr match {resultScore(result)}/100</span>{/if}
                  </div>
                  <button
                    type="button"
                    class="btn btn-sm min-h-11 min-w-11 shrink-0 preset-tonal-secondary"
                    aria-label={`${librarrAdded.has(resultIdentity(result)) ? "Added" : "Add"} ${resultTitle(result)} to Librarr`}
                    disabled={librarrAddingKey !== null || librarrBulkAdding ||
                      librarrAdded.has(resultIdentity(result))}
                    aria-busy={librarrAddingKey === resultIdentity(result)}
                    onclick={() => void addLibrarrResult(result, index)}
                    >{#if librarrAddingKey === resultIdentity(result)}<RefreshCw
                        size={15}
                        class="animate-spin"
                      />{:else if librarrAdded.has(resultIdentity(result))}<Check
                        size={15}
                      />{:else}<Library size={15} />{/if}<span
                      class="hidden sm:inline"
                      >{librarrAdded.has(resultIdentity(result)) ? "Added" : "Add"}</span
                    ></button
                  >
                </article>
              {/each}
            </div>
          {/if}
        </div>
      </dialog>
    </div>
  {/if}
</div>
