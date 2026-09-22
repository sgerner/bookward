<script lang="ts">
  import { applyAction, deserialize, enhance } from "$app/forms";
  import type { SubmitFunction } from "@sveltejs/kit";
  import { invalidateAll, pushState } from "$app/navigation";
  import { page } from "$app/state";
  import { onMount, tick } from "svelte";
  import { flip } from "svelte/animate";
  import { fade, fly, scale, slide } from "svelte/transition";
  import {
    ArrowRight,
    Bell,
    BookOpen,
    Bookmark,
    Check,
    ChevronDown,
    CircleHelp,
    Compass,
    Copy,
    ExternalLink,
    KeyRound,
    Library,
    Link2,
    Mail,
    MessageCircle,
    RefreshCw,
    Search,
    Send,
    Settings2,
    Sparkles,
    TriangleAlert,
    Trash2,
    Upload,
    X,
  } from "@lucide/svelte";
  import ThemePicker from "$lib/components/ThemePicker.svelte";
  import LlmSettings from "$lib/components/LlmSettings.svelte";
  import { copyApiTokenText } from "$lib/api-token-clipboard";
  import { tokenForView } from "$lib/api-token-ui";
  import { createTelemetryClient } from "$lib/telemetry";
  import bookwardMark from "$lib/assets/bookward-mark.svg";

  type View = "discover" | "saved" | "sources" | "settings";
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
    llmCatalog?: any;
    deviceLogin?: any;
    llmRuns?: any[];
  } | null | undefined;
  type MediaType = "ebook" | "audiobook";
  type SourceFilter = "all" | "permanent" | "one_time";
  type LibrarrResult = Record<string, unknown>;

  let { data, form } = $props();
  type PageBook = (typeof data.books)[number];
  let activeView = $state<View>(readView(page.url.searchParams.get("view")));
  let filter = $state("");
  let searchOpen = $state(false);
  let searchInput = $state<HTMLInputElement | null>(null);
  let detailsId = $state<number | null>(null);
  const DISCOVER_PAGE_SIZE = 8;
  let discoverVisibleCount = $state(DISCOVER_PAGE_SIZE);
  let loadMoreSentinel = $state<HTMLElement | null>(null);
  let additionalDiscoverBooks = $state<PageBook[]>([]);
  let discoverHasMore = $state(true);
  let discoverLoading = $state(false);
  let discoverLoadError = $state("");
  // This is only an identity sentinel. Keeping it outside `$state` avoids
  // proxying `data.books` and retriggering the synchronization effect forever.
  let previousDataBooks: PageBook[] | null = null;
  let pendingAction = $state<string | null>(null);
  let sourceFilter = $state<SourceFilter>("all");
  let librarrSearchOpen = $state(false);
  let librarrSearchBook = $state<{
    id: number;
    title: string;
    author: string;
  } | null>(null);
  let librarrQuery = $state("");
  let librarrMediaType = $state<MediaType>("audiobook");
  let librarrResults = $state<LibrarrResult[]>([]);
  let librarrSearchState = $state<"idle" | "searching" | "ready" | "error">(
    "idle",
  );
  let librarrSearchError = $state("");
  let librarrSearchMessage = $state("");
  let librarrAddingIndex = $state<number | null>(null);
  let librarrAdded = $state<Set<number>>(new Set());
  let digestMode = $state(page.url.searchParams.get("digest") === "1");
  let selectedDigestIds = $state<Set<number>>(new Set());
  let digestEnabledOverride = $state<boolean | null>(null);
  let digestOnlyNewOverride = $state<boolean | null>(null);
  let digestDiscordOverride = $state<boolean | null>(null);
  let digestEmailOverride = $state<boolean | null>(null);
  let apiTokenCopyMessage = $state<string | null>(null);
  let revealedApiToken = $state<string | null>(null);
  const telemetry = createTelemetryClient();

  const navItems: NavItem[] = [
    {
      id: "discover",
      label: "Discover",
      shortLabel: "Discover",
      icon: Compass,
    },
    { id: "saved", label: "Shortlist", shortLabel: "Saved", icon: Bookmark },
    { id: "sources", label: "Sources", shortLabel: "Sources", icon: Link2 },
    {
      id: "settings",
      label: "Settings",
      shortLabel: "Settings",
      icon: Settings2,
    },
  ];
  function matchesBookFilter(book: PageBook) {
    const query = filter.trim().toLowerCase();
    return (
      !query ||
      `${book.title} ${book.author} ${book.genres.join(" ")}`
        .toLowerCase()
        .includes(query)
    );
  }
  const allBooks = $derived([...data.books, ...additionalDiscoverBooks]);
  const discoverBooks = $derived(
    allBooks.filter(
      (book) => book.status === "recommended" && matchesBookFilter(book),
    ),
  );
  const savedBooks = $derived(
    allBooks.filter(
      (book) =>
        ["saved", "imported"].includes(book.status) && matchesBookFilter(book),
    ),
  );
  const filteredBooks = $derived(
    activeView === "saved" ? savedBooks : discoverBooks,
  );
  const visibleBooks = $derived(
    activeView === "discover"
      ? filteredBooks.slice(0, discoverVisibleCount)
      : filteredBooks,
  );
  const selectedDigestCount = $derived(selectedDigestIds.size);
  const digestSettings = $derived(data.profile.digest);
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
  const digestVisible = $derived(
    digestMode &&
      (!page.url.searchParams.has("digest_period") || data.digestReview.requested),
  );
  const hasMoreDiscoverBooks = $derived(
    activeView === "discover" &&
      (visibleBooks.length < filteredBooks.length || discoverHasMore),
  );
  const savedCount = $derived(
    allBooks.filter((book) => ["saved", "imported"].includes(book.status))
      .length,
  );
  const activeSourceCount = $derived(
    data.sources.filter((source) => source.enabled).length +
      (data.profile.default_source_enabled ? 1 : 0),
  );
  const permanentSources = $derived(
    data.sources.filter((source) => source.lifecycle !== "one_time"),
  );
  const oneTimeSources = $derived(
    data.sources.filter((source) => source.lifecycle === "one_time"),
  );
  const activePermanentSourceCount = $derived(
    permanentSources.filter((source) => source.enabled).length,
  );
  const activeOneTimeSourceCount = $derived(
    oneTimeSources.filter((source) => source.enabled).length,
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
  const formIsError = $derived(Boolean(formState?.error));

  function readView(value: string | null): View {
    return value && ["discover", "saved", "sources", "settings"].includes(value)
      ? (value as View)
      : "discover";
  }
  function go(view: View) {
    revealedApiToken = tokenForView(view, revealedApiToken);
    activeView = view;
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("view", view);
      if (view !== "discover") url.searchParams.delete("digest");
      pushState(url, {});
    }
  }
  function setPending(key: string): SubmitFunction {
    return () => {
      pendingAction = key;
      return async ({ update }) => {
        try {
          await update();
        } finally {
          pendingAction = null;
        }
      };
    };
  }
  function setPendingDigestBulk(): SubmitFunction {
    return () => {
      pendingAction = "shortlist-bulk";
      return async ({ update }) => {
        try {
          await update();
          clearDigestSelection();
        } finally {
          pendingAction = null;
        }
      };
    };
  }
  function isPending(key: string) {
    return pendingAction === key;
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

  async function loadMoreDiscover() {
    if (discoverLoading) return;
    if (discoverVisibleCount < filteredBooks.length) {
      discoverVisibleCount = Math.min(
        discoverVisibleCount + DISCOVER_PAGE_SIZE,
        filteredBooks.length,
      );
      return;
    }
    if (!discoverHasMore) return;

    discoverLoading = true;
    discoverLoadError = "";
    const offset = allBooks.filter((book) => book.status === "recommended").length;
    try {
      const params = new URLSearchParams({
        status: "recommended",
        limit: String(DISCOVER_PAGE_SIZE),
        offset: String(offset),
      });
      const response = await fetch(`/api/recommendations?${params.toString()}`);
      const payload = (await response.json().catch(() => ({}))) as {
        items?: PageBook[];
        has_more?: boolean;
        message?: string;
      };
      if (!response.ok || !Array.isArray(payload.items)) {
        throw new Error(payload.message || "Recommendations could not be loaded.");
      }
      const existing = new Set(allBooks.map((book) => book.id));
      additionalDiscoverBooks = [
        ...additionalDiscoverBooks,
        ...payload.items.filter((book) => !existing.has(book.id)),
      ];
      discoverHasMore = payload.has_more === true;
      discoverVisibleCount += DISCOVER_PAGE_SIZE;
    } catch (error) {
      discoverLoadError = error instanceof Error ? error.message : "Recommendations could not be loaded.";
    } finally {
      discoverLoading = false;
    }
  }

  function toggleSearch() {
    searchOpen = !searchOpen;
    if (searchOpen) void tick().then(() => searchInput?.focus());
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
    if (status?.startsWith("error:")) return "Needs attention";
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
    return resultText(
      result,
      ["media_type", "format", "type"],
      librarrMediaType,
    );
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
    return `${resultText(
      result,
      ["id", "guid", "isbn", "asin"],
      resultTitle(result),
    )}-${index}`;
  }
  function openLibrarrSearch(book: {
    id: number;
    title: string;
    author: string;
  }) {
    if (!data.profile.librar_connected) return;
    librarrSearchBook = book;
    librarrQuery = `${book.title} ${book.author}`.trim().slice(0, 200);
    librarrMediaType =
      data.profile.librarr_media_type === "ebook" ? "ebook" : "audiobook";
    librarrResults = [];
    librarrSearchError = "";
    librarrSearchMessage = "";
    librarrAdded = new Set();
    librarrSearchOpen = true;
    void searchLibrarr();
  }
  function closeLibrarrSearch() {
    librarrSearchOpen = false;
  }

  function recordBookEvent(
    candidateId: number,
    eventType: "visible" | "detail_open" | "source_open" | "librarr_search" | "librarr_import",
    metadata: Record<string, unknown> = {},
  ) {
    const runId = String(data.recommendation_run_id || "");
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
    const nextBooks = data.books;
    if (previousDataBooks === nextBooks) return;
    previousDataBooks = nextBooks;
    additionalDiscoverBooks = [];
    discoverHasMore = true;
    discoverLoadError = "";
  });

  $effect(() => {
    activeView;
    filter;
    discoverVisibleCount = DISCOVER_PAGE_SIZE;
  });

  // A delivery link can outlive its notification history. Keep that case
  // useful by showing the normal Discover feed instead of an empty digest.
  $effect(() => {
    if (
      digestMode &&
      page.url.searchParams.has("digest_period") &&
      !data.digestReview.requested
    ) {
      digestMode = false;
    }
  });

  $effect(() => {
    const sentinel = loadMoreSentinel;
    if (
      !sentinel ||
      !hasMoreDiscoverBooks ||
      typeof IntersectionObserver === "undefined"
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMoreDiscover();
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  });
  async function searchLibrarr() {
    const query = librarrQuery.trim();
    if (query.length < 2) {
      librarrSearchState = "error";
      librarrSearchError = "Enter at least two characters.";
      return;
    }
    librarrSearchState = "searching";
    librarrSearchError = "";
    librarrSearchMessage = "";
    try {
      const params = new URLSearchParams({
        q: query,
        media_type: librarrMediaType,
      });
      if (librarrSearchBook) recordBookEvent(librarrSearchBook.id, "librarr_search", { query });
      const response = await fetch(`/api/librarr/search?${params.toString()}`);
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
      librarrSearchState = "ready";
    } catch (error) {
      librarrSearchState = "error";
      librarrSearchError =
        error instanceof Error ? error.message : "Librarr search failed.";
    }
  }
  async function addLibrarrResult(result: LibrarrResult, index: number, closeAfter = true) {
    if (librarrAdded.has(index)) return;
    librarrAddingIndex = index;
    librarrSearchError = "";
    librarrSearchMessage = "";
    try {
      const response = await fetch("/api/librarr/download", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ media_type: librarrMediaType, candidate_id: librarrSearchBook?.id, result }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        message?: string;
      };
      if (!response.ok)
        throw new Error(payload.message || "Librarr could not add that book.");
      librarrAdded = new Set([...librarrAdded, index]);
      if (librarrSearchBook) recordBookEvent(librarrSearchBook.id, "librarr_import", { result_index: index });
      librarrSearchMessage = `${resultTitle(result)} added to Librarr.`;
      if (closeAfter) {
        closeLibrarrSearch();
        await invalidateAll();
      }
      return true;
    } catch (error) {
      librarrSearchError =
        error instanceof Error
          ? error.message
          : "Librarr could not add that book.";
      return false;
    } finally {
      librarrAddingIndex = null;
    }
  }
  async function addHighConfidenceLibrarrResults() {
    if (!highConfidenceLibrarrResults.length || librarrAddingIndex !== null) return;
    librarrAddingIndex = -1;
    librarrSearchError = "";
    librarrSearchMessage = "";
    let added = 0;
    for (const item of highConfidenceLibrarrResults) {
      const success = await addLibrarrResult(item.result, item.index, false);
      if (success) added += 1;
    }
    librarrAddingIndex = null;
    if (added) {
      closeLibrarrSearch();
      await invalidateAll();
    }
  }

  onMount(() => {
    const onPopState = () => {
      const nextView = readView(
        new URL(window.location.href).searchParams.get("view"),
      );
      revealedApiToken = tokenForView(nextView, revealedApiToken);
      activeView = nextView;
      digestMode = new URL(window.location.href).searchParams.get("digest") === "1";
    };
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && librarrSearchOpen) closeLibrarrSearch();
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("keydown", onKeydown);
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
                run_id: String(data.recommendation_run_id || ""),
              });
              const response = await fetch("?/decide", {
                method: "POST",
                headers: {
                  accept: "application/json",
                  "content-type": "application/x-www-form-urlencoded",
                  "x-sveltekit-action": "true",
                },
                body,
              });
              if (!response.ok)
                throw new Error("Could not shortlist that book.");
              await applyAction(deserialize(await response.text()));
              await invalidateAll();
              go("saved");
              return { id, status: "saved" };
            },
          },
          { signal: lifecycle.signal },
        ),
      ).catch(() => undefined);
    }
    return () => {
      lifecycle.abort();
      void telemetry.flush({ beacon: true });
      telemetry.destroy();
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("keydown", onKeydown);
    };
  });

  $effect(() => {
    if (activeView !== "discover" && activeView !== "saved") searchOpen = false;
  });
</script>

<svelte:head>
  <title>Bookward — your next great read</title>
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
  class="relative isolate min-h-screen overflow-x-clip text-surface-900-100 before:pointer-events-none before:fixed before:inset-0 before:-z-10 before:content-[''] before:bg-[radial-gradient(circle_at_14%_2%,_color-mix(in_oklab,_var(--color-primary-500)_72%,_transparent)_0%,_transparent_36%),radial-gradient(circle_at_86%_8%,_color-mix(in_oklab,_var(--color-secondary-500)_60%,_transparent)_0%,_transparent_32%),radial-gradient(circle_at_52%_100%,_color-mix(in_oklab,_var(--color-tertiary-500)_64%,_transparent)_0%,_transparent_44%)] before:opacity-70 after:pointer-events-none after:fixed after:inset-0 after:-z-10 after:content-[''] after:bg-[linear-gradient(118deg,_transparent_0%,_color-mix(in_oklab,_var(--color-primary-500)_22%,_transparent)_42%,_transparent_68%),linear-gradient(180deg,_transparent_50%,_color-mix(in_oklab,_var(--color-secondary-500)_18%,_transparent)_100%)] after:opacity-90 dark:before:opacity-90 dark:after:opacity-100"
>
  <header
    class="sticky top-0 z-40 border-b border-primary-500/15 preset-filled-surface-50-950 shadow-lg shadow-primary-500/5 backdrop-blur-xl"
  >
    <div
      class="relative mx-auto flex min-h-16 max-w-7xl items-center gap-6 px-4 sm:px-6 lg:px-8"
    >
      <a
        href="/"
        class="flex min-h-11 shrink-0 items-center gap-3 text-surface-950-50 no-underline"
        aria-label="Bookward home"
        ><span class="grid size-10 overflow-hidden rounded-2xl shadow-lg shadow-primary-500/20"
          ><img src={bookwardMark} alt="" class="size-full object-cover" aria-hidden="true" /></span
        ><span class="hidden text-lg font-semibold tracking-tight sm:inline"
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
            ><Icon size={16} strokeWidth={1.8} /><span>{item.label}</span
            >{#if item.id === "saved" && savedCount > 0}<span
                in:scale={{ duration: motionDuration(160) }}
                out:fade={{ duration: motionDuration(100) }}
                class="badge badge-sm preset-tonal-surface">{savedCount}</span
              >{/if}</button
      >{/each}
      </nav>
      <div class="ml-auto flex items-center gap-2">
        {#if activeView === "discover" || activeView === "saved"}
          {#if searchOpen}
            <form
              class="flex items-center gap-1"
              onsubmit={(event) => event.preventDefault()}
              in:fade={{ duration: motionDuration(180) }}
              out:fade={{ duration: motionDuration(120) }}
            >
              <label class="input flex min-h-11 w-[min(18rem,58vw)] items-center gap-2">
                <Search size={16} class="shrink-0 text-surface-600-400" />
                <input
                  bind:this={searchInput}
                  bind:value={filter}
                  class="input-ghost min-w-0"
                  aria-label="Search books"
                  placeholder="Search books"
                />
              </label>
              <button
                type="button"
                class="btn btn-icon min-h-11 min-w-11 preset-tonal-surface"
                aria-label="Close search"
                onclick={toggleSearch}><X size={16} /></button
              >
            </form>
          {:else}<button
              type="button"
              class="btn btn-icon min-h-11 min-w-11 preset-tonal-surface"
              aria-label="Search books"
              onclick={toggleSearch}
              in:fade={{ duration: motionDuration(180) }}
              out:fade={{ duration: motionDuration(120) }}
              ><Search size={17} /></button
            >{/if}
        {/if}
        {#if activeView === "discover" || activeView === "saved"}<form
            method="POST"
            action="?/runSync"
            use:enhance={setPending("sync")}
            in:fade={{ duration: motionDuration(180) }}
            out:fade={{ duration: motionDuration(120) }}
          >
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
        <ThemePicker />
      </div>
    </div>
  </header>

  <div
    class="mx-auto flex max-w-7xl gap-10 px-4 pb-28 pt-8 sm:px-6 lg:px-8 lg:pb-14 lg:pt-12"
  >
    <div id="main-content" role="main" class="min-w-0 flex-1">
      {#if formState?.message}{#key formState.message}<div
            in:fly={{ y: -12, duration: motionDuration(240) }}
            out:fade={{ duration: motionDuration(140) }}
            class={`mb-6 flex items-start gap-3 border p-4 text-sm ${formIsError ? "preset-tonal-error" : "preset-tonal-success"}`}
            role={formIsError ? "alert" : "status"}
          >
            {#if formIsError}<CircleHelp
                size={18}
                class="mt-0.5 shrink-0"
              />{:else}<Check size={18} class="mt-0.5 shrink-0" />{/if}<span
              >{formState.message}</span
            >
          </div>{/key}{/if}

      {#snippet recommendationView(view: "discover" | "saved")}

        {@const viewBooks = view === "discover" ? (digestVisible ? (data.digestReview.requested ? discoverBooks.filter((book) => data.digestReview.ids.includes(book.id)) : discoverBooks.filter((book) => book.score >= digestSettings.minimum_score)) : discoverBooks) : savedBooks}
        {@const viewVisibleBooks = view === "discover" ? viewBooks.slice(0, discoverVisibleCount) : viewBooks}
        {@const viewHasMoreDiscoverBooks = view === "discover" && (viewVisibleBooks.length < viewBooks.length || discoverHasMore)}
        <section class="mb-10 px-1 sm:px-0">
          <h1
            class="text-4xl font-semibold leading-[1.05] tracking-tight text-surface-950-50 sm:text-6xl"
          >{view === "discover" ? "Find your next favorite." : "Your shortlist."}</h1>
        </section>
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
                  <input type="hidden" name="run_id" value={data.recommendation_run_id} />
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
        <section class="grid gap-5 lg:grid-cols-2" aria-live="polite">
          {#each viewVisibleBooks as book, index (book.id)}
            {@const releaseLabel = formatRelease(book.published_on, book.published_kind)}
            <article
              use:trackRecommendation={{ candidateId: book.id }}
              in:fly={{ y: 18, duration: motionDuration(380), delay: motionDelay(index) }}
              out:fade={{ duration: motionDuration(160) }}
              animate:flip={{ duration: motionDuration(360) }}
              class={`relative card group grid grid-cols-[7rem_minmax(0,1fr)] overflow-hidden bg-gradient-to-br from-primary-500/8 via-transparent to-secondary-500/8 preset-tonal-surface shadow-lg shadow-primary-500/5 transition duration-300 hover:-translate-y-1 hover:shadow-2xl hover:shadow-primary-500/10 sm:grid-cols-[10rem_minmax(0,1fr)] ${index === 0 && view === "discover" ? "lg:col-span-2 lg:grid-cols-[12rem_minmax(0,1fr)]" : ""}`}
            >
              {#if view === "discover" && digestVisible && book.status === "recommended"}
                <label class="absolute left-3 top-3 z-30 grid size-8 cursor-pointer place-items-center preset-filled-surface-50-950 opacity-90 shadow-lg" title={`Select ${book.title}`}>
                  <input
                    class="checkbox size-5 accent-secondary-500"
                    type="checkbox"
                    checked={selectedDigestIds.has(book.id)}
                    onchange={() => toggleDigestBook(book.id)}
                    aria-label={`Select ${book.title} for shortlist`}
                  />
                </label>
              {/if}
              <button
                type="button"
                class={`relative aspect-[2/3] h-fit w-full self-start overflow-hidden preset-tonal-surface text-left before:pointer-events-none before:absolute before:inset-0 before:z-10 before:bg-gradient-to-t before:from-primary-950/30 before:via-transparent before:to-secondary-500/10 before:content-[''] sm:aspect-auto sm:h-full sm:self-stretch ${index === 0 && view === "discover" ? "lg:aspect-auto lg:h-full lg:self-stretch" : ""}`}
                onclick={() => (detailsId = detailsId === book.id ? null : book.id)}
                aria-label={`View details for ${book.title}`}
                aria-expanded={detailsId === book.id}
              >
                <span class="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center text-surface-600-400">
                  <BookOpen size={28} /><span class="line-clamp-3 text-xs">{book.title}</span>
                </span>
                {#if book.cover_url}<img
                    in:fade={{ duration: motionDuration(340) }}
                    class="absolute inset-0 h-full w-full object-cover transition duration-500 group-hover:scale-[1.05]"
                    src={book.cover_url}
                    alt={`Cover of ${book.title}`}
                    loading={index > 2 ? "lazy" : "eager"}
                    onerror={(event) => ((event.currentTarget as HTMLImageElement).hidden = true)}
                  />{/if}
                <span in:scale={{ duration: motionDuration(160) }} class="badge absolute bottom-3 left-3 z-20 preset-filled-primary-500">{book.score}% match</span>
              </button>
              <div class="flex min-w-0 flex-col gap-2.5 p-4 sm:p-5">
                <div class="flex flex-wrap items-center gap-2 text-sm font-medium text-surface-600-400">
                  {#if releaseLabel}<span>{releaseLabel}</span>{/if}
                  {#each book.genres.slice(0, 2) as genre (genre)}<span in:scale={{ duration: motionDuration(150) }} class="badge badge-sm preset-tonal-secondary">{genre}</span>{/each}
                  {#if book.source_url}<a
                      in:scale={{ duration: motionDuration(180) }}
                      class="btn btn-icon btn-xs preset-tonal-surface ml-auto shrink-0"
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
                  open={detailsId === book.id}
                  ontoggle={(event) => {
                    const open = (event.currentTarget as HTMLDetailsElement).open;
                    if (open) recordBookEvent(book.id, "detail_open");
                    detailsId = open ? book.id : null;
                  }}
                >
                  <summary class="flex min-h-11 cursor-pointer list-none items-center gap-2 font-medium text-primary-600-400">
                    <Sparkles size={15} /><span>Why this might be for you</span><ChevronDown size={15} class="ml-auto transition group-open/details:rotate-180" />
                  </summary>
                  {#if detailsId === book.id}<div in:slide={{ duration: motionDuration(220) }} out:fade={{ duration: motionDuration(120) }}>
                      <p class="mt-2 leading-6">{book.reason}</p>
                      {#if book.synopsis}<div class="mt-4 border-l-2 border-secondary-500/50 pl-3">
                          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-600-400">Book summary</p>
                          <p class="mt-1 leading-6 text-surface-800-200">{book.synopsis}</p>
                        </div>{/if}
                    </div>{/if}
                </details>
                <div class="mt-auto flex flex-wrap gap-2 pt-1">
                  {#if book.status === "recommended"}
                    {#if data.profile.librar_connected}<button in:fly={{ y: 8, duration: motionDuration(180) }} type="button" class="btn btn-sm min-h-11 preset-tonal-secondary" onclick={() => openLibrarrSearch(book)}><Search size={15} /> Find in Librarr</button>{/if}
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(1, 20) }} method="POST" action="?/decide" use:enhance={setPending(`save-${book.id}`)}>
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="saved" /><input type="hidden" name="run_id" value={data.recommendation_run_id} /><button type="submit" class="btn btn-sm min-h-11 preset-filled-primary-500" aria-busy={isPending(`save-${book.id}`)}>{#if isPending(`save-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<Bookmark size={15} />{/if} Shortlist</button>
                    </form>
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(2, 20) }} method="POST" action="?/decide" use:enhance={setPending(`pass-${book.id}`)}>
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="rejected" /><input type="hidden" name="run_id" value={data.recommendation_run_id} /><button type="submit" class="btn btn-sm min-h-11 preset-tonal-surface" aria-label={`Pass on ${book.title}`} aria-busy={isPending(`pass-${book.id}`)}>{#if isPending(`pass-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<X size={15} />{/if} Pass</button>
                    </form>
                  {:else if book.status === "saved"}
                    {#if data.profile.librar_connected}<button in:fly={{ y: 8, duration: motionDuration(180) }} type="button" class="btn btn-sm min-h-11 preset-tonal-secondary" onclick={() => openLibrarrSearch(book)}><Search size={15} /> Find in Librarr</button>{/if}
                    <form in:fly={{ y: 8, duration: motionDuration(180) }} method="POST" action="?/importLibrar" use:enhance={setPending(`import-${book.id}`)}>
                      <input type="hidden" name="id" value={book.id} /><button type="submit" class="btn btn-sm min-h-11 preset-filled-primary-500" disabled={!data.profile.librar_connected || isPending(`import-${book.id}`)} aria-busy={isPending(`import-${book.id}`)}>{#if isPending(`import-${book.id}`)}<RefreshCw size={15} class="animate-spin" />{:else}<Library size={15} />{/if} {data.profile.librar_connected ? `Add ${data.profile.librarr_media_type === "ebook" ? "ebook" : "audiobook"} to waitlist` : "Connect Librarr first"}</button>
                    </form>
                    <form in:fly={{ y: 8, duration: motionDuration(180), delay: motionDelay(2, 20) }} method="POST" action="?/decide" use:enhance={setPending(`restore-${book.id}`)}>
                      <input type="hidden" name="id" value={book.id} /><input type="hidden" name="status" value="recommended" /><input type="hidden" name="run_id" value={data.recommendation_run_id} /><button type="submit" class="btn btn-sm min-h-11 preset-tonal-surface" aria-busy={isPending(`restore-${book.id}`)}>Remove</button>
                    </form>
                  {:else}<span in:scale={{ duration: motionDuration(180) }} class="badge min-h-11 preset-tonal-success"><Check size={15} /> Added to Librarr</span>{/if}
                </div>
              </div>
            </article>
          {:else}<div in:scale={{ duration: motionDuration(260) }} class="card col-span-full flex min-h-72 flex-col items-center justify-center gap-4 border-dashed preset-tonal-surface p-8 text-center">
              <span class="grid size-14 place-items-center rounded-full preset-tonal-primary"><BookOpen size={24} /></span>
              <div>
                <h2 class="text-lg font-semibold text-surface-950-50">{filter ? "No books match that search" : view === "saved" ? "Your shortlist is empty" : digestVisible ? "No digest picks yet" : "You are all caught up"}</h2>
                <p class="mt-1 max-w-sm text-sm leading-6 text-surface-700-300">{filter ? "Try an author, title, or genre." : view === "saved" ? "Shortlist a recommendation when one catches your eye." : digestVisible ? "The next digest will appear here when a new book clears your match threshold." : "Refresh your sources or check back when your next set of books is ready."}</p>
              </div>
              {#if view === "saved"}<button in:fly={{ y: 8, duration: motionDuration(220) }} type="button" class="btn preset-filled-primary-500" onclick={() => go("discover")}>Browse recommendations <ArrowRight size={16} /></button>{/if}
            </div>{/each}
          {#if viewHasMoreDiscoverBooks}<div bind:this={loadMoreSentinel} class="col-span-full flex justify-center pt-1">
              <button type="button" class="btn btn-sm preset-tonal-secondary" onclick={loadMoreDiscover} disabled={discoverLoading} aria-busy={discoverLoading}>{#if discoverLoading}<RefreshCw size={14} class="animate-spin" />{:else}<ArrowRight size={14} />{/if} {discoverLoadError ? "Try again" : "Load more"}</button>
            </div>{/if}
        </section>
      {/snippet}

      <!-- Layer each view in the same grid cell so fade intros/outros overlap instead of leaving a blank frame. -->
      <div class="grid min-h-[20rem]">

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
        {:else if activeView === "sources"}
          <div
            data-view="sources"
            class="col-start-1 row-start-1"
            in:fade={{ duration: motionDuration(420) }}
            out:fade={{ duration: motionDuration(420) }}
          >
            <section class="mb-6 max-w-2xl">
              <h1
                class="text-4xl font-semibold tracking-tight text-surface-950-50 sm:text-5xl"
              >
                Choose your sources.
              </h1>
            </section>
            <section
              class="grid gap-5 lg:grid-cols-[minmax(0,1.5fr)_minmax(18rem,1fr)]"
            >
              <div
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(0),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
              >
                <div class="mb-4 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
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
                    class="flex w-full items-center gap-1 preset-tonal-surface p-1 sm:w-auto"
                    role="tablist"
                    aria-label="Filter sources by lifecycle"
                  >
                    {#each sourceFilterOptions as option}
                      <button
                        type="button"
                        role="tab"
                        aria-selected={sourceFilter === option.value}
                        class={`min-h-9 flex-1 px-3 text-xs font-medium transition sm:flex-none ${sourceFilter === option.value ? "preset-filled-secondary-500" : "text-surface-700-300 hover:preset-tonal-secondary"}`}
                        onclick={() => (sourceFilter = option.value)}
                      >
                        {option.label}
                        <span class="ml-1 opacity-70">{option.count}</span>
                      </button>
                    {/each}
                  </div>
                </div>
                <div class="divide-y divide-surface-200-800">
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
                        use:enhance={setPending("toggle-default")}
                      >
                        <button
                          type="submit"
                          class={`relative h-7 w-12 rounded-full p-1 transition ${data.profile.default_source_enabled ? "preset-filled-primary-500" : "preset-filled-surface-500"}`}
                          role="switch"
                          aria-label="Toggle Bookward demo books"
                          aria-checked={!!data.profile.default_source_enabled}
                          aria-busy={isPending("toggle-default")}
                          ><span
                            class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition ${data.profile.default_source_enabled ? "translate-x-5" : ""}`}
                          ></span></button
                        >
                      </form>
                    </div>
                  {/if}
                  {#key sourceFilter}
                    <div in:fade={{ duration: motionDuration(160) }} out:fade={{ duration: motionDuration(100) }}>
                  {#each visibleSourceGroups as group}
                    <div
                      class="flex items-end justify-between gap-4 border-b border-surface-200-800 px-1 py-4"
                    >
                      <div>
                        <h3 class="font-semibold text-surface-950-50">
                          {group.label}
                        </h3>
                        <p class="mt-1 text-sm text-surface-700-300">
                          {group.description}
                        </p>
                      </div>
                      <span class="badge shrink-0 preset-tonal-surface"
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
                          class="flex min-h-24 items-center gap-4 border-b border-surface-200-800 py-4 last:border-b-0"
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
                            <span class="mt-1 block text-xs text-surface-600-400"
                              >{source.lifecycle === "one_time" ? "One-time import" : "Permanent feed"} · {formatSourceScan(source.last_scanned_at, source.last_status)}</span
                            >
                          </div>
                          <form
                            method="POST"
                            action="?/toggleSource"
                            use:enhance={setPending(`toggle-source-${source.id}`)}
                          >
                            <input
                              type="hidden"
                              name="id"
                              value={source.id}
                            /><button
                              type="submit"
                              class={`relative h-7 w-12 rounded-full p-1 transition ${source.enabled ? "preset-filled-primary-500" : "preset-filled-surface-500"}`}
                              role="switch"
                              aria-label={`Toggle ${source.label}`}
                              aria-checked={!!source.enabled}
                              aria-busy={isPending(`toggle-source-${source.id}`)}
                              ><span
                                class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition ${source.enabled ? "translate-x-5" : ""}`}
                              ></span></button
                            >
                          </form>
                        </div>{/each}
                    {:else}
                      <div class="border-b border-surface-200-800 px-1 py-5 text-sm text-surface-600-400">
                        No one-time imports yet. Add a source when you want a
                        single snapshot.
                      </div>
                    {/if}
                  {/each}
                    </div>
                  {/key}
                </div>
              </div>
              <div class="space-y-5">
                <form
                  in:fly={{
                    y: 12,
                    duration: motionDuration(300),
                    delay: motionDelay(1),
                  }}
                  class="card preset-tonal-surface p-5 sm:p-6"
                  method="POST"
                  action="?/configureSourceSchedule"
                  use:enhance={setPending("source-schedule")}
                >
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
                      class="select mt-2"
                      name="intervalHours"
                    >
                      <option value="0" selected={data.profile.source_sync_interval_hours === 0}>Manual only</option>
                      <option value="6" selected={data.profile.source_sync_interval_hours === 6}>Every 6 hours</option>
                      <option value="24" selected={data.profile.source_sync_interval_hours === 24}>Daily (UTC)</option>
                      <option value="168" selected={data.profile.source_sync_interval_hours === 168}>Weekly</option>
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
                  class="card h-fit preset-tonal-surface p-5 sm:p-6"
                  method="POST"
                  action="?/source"
                  use:enhance={setPending("source")}
                >
                  <span
                    class="mb-4 grid size-10 place-items-center preset-tonal-tertiary"
                    ><Link2 size={18} /></span
                  >
                  <h2 class="text-lg font-semibold text-surface-950-50">
                    Add a source
                  </h2>
                  <p class="mt-1 text-sm leading-6 text-surface-700-300">
                    Paste a public list, publisher page, newsletter archive, or
                    bookseller collection.
                  </p>
                  <label
                    class="mt-5 block text-sm font-medium text-surface-800-200"
                    >Name<input
                      class="input mt-2"
                      name="label"
                      placeholder="e.g. Reactor new releases"
                      required
                    /></label
                  ><label
                    class="mt-4 block text-sm font-medium text-surface-800-200"
                    >Public URL<input
                      class="input mt-2"
                      name="url"
                      type="url"
                      placeholder="https://…"
                      required
                    /></label
                  ><label
                    class="mt-4 block text-sm font-medium text-surface-800-200"
                    >Source type<select class="select mt-2" name="lifecycle">
                      <option value="permanent">Permanent feed · keep it fresh</option>
                      <option value="one_time">One-time import · scan once</option>
                    </select></label
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
                  Pull recent reads from a public read-shelf feed.
                </p>
                <label
                  class="mt-5 block text-sm font-medium text-surface-800-200"
                  >Public shelf RSS URL<input
                    class="input mt-2"
                    name="url"
                    type="url"
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
                    />{:else}<RefreshCw size={16} />{/if} Sync RSS now</button
                >
              </form>
              <form
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(1),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
                method="POST"
                action="?/importGoodreadsCsv"
                enctype="multipart/form-data"
                use:enhance={setPending("goodreads-csv")}
              >
                <div class="mb-5 flex items-center justify-between gap-4">
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"
                      ><Upload size={19} /></span
                    >
                    <h2
                      class="truncate text-lg font-semibold text-surface-950-50"
                    >
                      Goodreads export
                    </h2>
                  </div>
                  <span class="badge shrink-0 preset-tonal-surface"
                    >Full history</span
                  >
                </div>
                <p class="mt-1 text-sm leading-6 text-surface-700-300">
                  Import your complete library, ratings, and read dates.
                </p>
                <label
                  class="mt-5 block text-sm font-medium text-surface-800-200"
                  >Goodreads CSV<input
                    class="input mt-2"
                    name="file"
                    type="file"
                    accept=".csv,text/csv"
                    required
                  /></label
                ><button
                  class="btn mt-5 min-h-11 w-full preset-filled-tertiary-500"
                  type="submit"
                  disabled={isPending("goodreads-csv")}
                  aria-busy={isPending("goodreads-csv")}
                  >{#if isPending("goodreads-csv")}<RefreshCw
                      size={16}
                      class="animate-spin"
                    />{:else}<Upload size={16} />{/if} Import CSV</button
                >
              </form>
              <form
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(2),
                }}
                class="card preset-tonal-surface p-5 sm:p-6"
                method="POST"
                action="?/configureLibrar"
                use:enhance={setPending("librarr")}
              >
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
                    class={`badge shrink-0 ${data.profile.librar_connected ? "preset-tonal-success" : "preset-tonal-surface"}`}
                    >{data.profile.librar_connected
                      ? "Connected"
                      : "Not connected"}</span
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
                    value={data.profile.librar_url}
                    placeholder="http://librarr:5050"
                    required
                  /></label
                ><label
                  class="mt-4 block text-sm font-medium text-surface-800-200"
                  >API key<input
                    class="input mt-2"
                    name="apiKey"
                    type="password"
                    placeholder={data.profile.librar_connected
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
                      selected={data.profile.librarr_media_type === "audiobook"}
                      >Audiobook</option
                    ><option
                      value="ebook"
                      selected={data.profile.librarr_media_type === "ebook"}
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
                    >{data.profile.embedding_backend}</span
                  >
                </div>
                <form
                  id="embedding-settings"
                  class="space-y-4"
                  method="POST"
                  action="?/configureEmbeddings"
                  use:enhance={setPending("embeddings")}
                >
                  <label class="block text-sm font-medium text-surface-800-200"
                    >Provider<select class="select mt-2" name="backend"
                      ><option
                        value="local"
                        selected={data.profile.embedding_backend === "local"}
                        >Local CPU · zero download</option
                      ><option
                        value="fastembed"
                        selected={data.profile.embedding_backend ===
                          "fastembed"}>Local BGE · FastEmbed</option
                      ><option
                        value="ollama"
                        selected={data.profile.embedding_backend === "ollama"}
                        >Ollama / Qwen</option
                      ><option
                        value="openai-compatible"
                        selected={data.profile.embedding_backend ===
                          "openai-compatible"}>OpenAI-compatible API</option
                      ></select
                    ></label
                  ><label class="block text-sm font-medium text-surface-800-200"
                    >Model<input
                      class="input mt-2"
                      name="model"
                      value={data.profile.embedding_model}
                      placeholder="qwen3-embedding:0.6b"
                      required
                    /></label
                  ><label class="block text-sm font-medium text-surface-800-200"
                    >Endpoint<input
                      class="input mt-2"
                      name="url"
                      type="url"
                      value={data.profile.embedding_url}
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
                  <label class="block min-w-0 flex-1 text-sm font-medium text-surface-800-200">
                    Token name
                    <input class="input mt-2" name="name" maxlength="100" placeholder="Home Assistant" required />
                  </label>
                  <button class="btn min-h-11 preset-filled-primary-500" type="submit" disabled={isPending("create-api-token")} aria-busy={isPending("create-api-token")}>
                    {#if isPending("create-api-token")}<RefreshCw size={16} class="animate-spin" />{:else}<KeyRound size={16} />{/if} Generate token
                  </button>
                </form>

                {#if data.profile.api_tokens.length}
                  <div class="mt-6 border-t border-surface-300-700/40 pt-5">
                    <h3 class="text-sm font-semibold text-surface-950-50">Existing tokens</h3>
                    <ul class="mt-3 divide-y divide-surface-300-700/40 border border-surface-300-700/40">
                      {#each data.profile.api_tokens as token (token.id)}
                        <li class="flex flex-wrap items-center gap-3 p-3">
                          <div class="min-w-0 flex-1">
                            <p class="truncate text-sm font-medium text-surface-950-50">{token.name}</p>
                            <p class="mt-1 text-xs text-surface-700-300"><code>{token.token_prefix}…</code> · Created {token.created_at}</p>
                            {#if token.last_used_at}<p class="mt-1 text-xs text-surface-700-300">Last used {token.last_used_at}</p>{/if}
                          </div>
                          {#if token.revoked_at}
                            <span class="badge preset-tonal-error">Revoked</span>
                          {:else}
                            <form method="POST" action="?/revokeApiToken" use:enhance={setPending(`revoke-api-token-${token.id}`)}>
                              <input type="hidden" name="id" value={token.id} />
                              <button class="btn btn-sm min-h-9 preset-tonal-error" type="submit" disabled={isPending(`revoke-api-token-${token.id}`)} aria-busy={isPending(`revoke-api-token-${token.id}`)}>
                                {#if isPending(`revoke-api-token-${token.id}`)}<RefreshCw size={14} class="animate-spin" />{:else}<Trash2 size={14} />{/if} Revoke
                              </button>
                            </form>
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
                <form method="POST" action="?/configureDigest" use:enhance={setPending("digest-settings")}>
                  <div class="flex flex-wrap items-center gap-4 focus-within:ring-2 focus-within:ring-secondary-500 focus-within:ring-offset-4 focus-within:ring-offset-surface-950">
                    <input id="digest-enabled" class="sr-only" type="checkbox" name="enabled" checked={digestEnabled} role="switch" aria-label="Enable weekly digest" onchange={(event) => (digestEnabledOverride = (event.currentTarget as HTMLInputElement).checked)} />
                    <span class="grid size-11 shrink-0 place-items-center preset-tonal-secondary"><Bell size={20} /></span>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-3">
                        <h2 class="text-lg font-semibold text-surface-950-50">Weekly digest</h2>
                        <span class="badge preset-tonal-surface">{digestEnabled ? "On" : "Off"}</span>
                      </div>
                      <p class="mt-1 text-sm text-surface-700-300">A short list of your strongest new matches, on your schedule.</p>
                    </div>
                    <label for="digest-enabled" class="flex cursor-pointer items-center gap-3 text-sm font-semibold text-surface-950-50">
                      <span class="sr-only">Enable weekly digest</span>
                      <span class={`relative h-7 w-12 shrink-0 rounded-full p-1 transition ${digestEnabled ? "preset-filled-secondary-500" : "preset-filled-surface-500"}`}><span class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${digestEnabled ? "translate-x-5" : ""}`}></span></span>
                    </label>
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
                        <label class="mt-4 flex cursor-pointer items-center justify-between gap-4 border-t border-surface-300-700/40 pt-4 text-sm font-medium text-surface-800-200">
                          <span><span class="block text-surface-950-50">Only send new books</span><span class="mt-1 block text-xs font-normal text-surface-700-300">Skip recommendations already included in a digest.</span></span>
                          <span class="relative shrink-0 focus-within:ring-2 focus-within:ring-secondary-500 focus-within:ring-offset-2 focus-within:ring-offset-surface-950"><input class="sr-only" type="checkbox" name="onlyNew" checked={digestOnlyNew} role="switch" aria-label="Only send new books" onchange={(event) => (digestOnlyNewOverride = (event.currentTarget as HTMLInputElement).checked)} /><span class={`block h-7 w-12 rounded-full p-1 transition ${digestOnlyNew ? "preset-filled-secondary-500" : "preset-filled-surface-500"}`}><span class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${digestOnlyNew ? "translate-x-5" : ""}`}></span></span></span>
                        </label>
                      </section>

                      <section class="preset-tonal-surface p-4 sm:p-5">
                        <div class="mb-4">
                          <h3 class="font-semibold text-surface-950-50">Delivery</h3>
                          <p class="mt-1 text-sm text-surface-700-300">Choose one or both places to receive it.</p>
                        </div>
                        <div class="grid gap-3">
                          <div class="relative border border-surface-300-700/50 p-4 focus-within:ring-2 focus-within:ring-secondary-500 focus-within:ring-offset-2 focus-within:ring-offset-surface-950">
                            <input id="digest-discord" class="sr-only" type="checkbox" name="channels" value="discord" checked={digestDiscord} role="switch" aria-label="Send digest to Discord" onchange={(event) => (digestDiscordOverride = (event.currentTarget as HTMLInputElement).checked)} />
                            <label for="digest-discord" class="flex cursor-pointer items-center gap-3">
                              <span class="grid size-9 shrink-0 place-items-center preset-tonal-secondary"><MessageCircle size={17} /></span>
                              <span class="min-w-0 flex-1"><span class="block font-medium text-surface-950-50">Discord</span><span class="mt-0.5 block text-xs text-surface-700-300">Post to a webhook</span></span>
                              <span class={`relative h-7 w-12 shrink-0 rounded-full p-1 transition ${digestDiscord ? "preset-filled-secondary-500" : "preset-filled-surface-500"}`}><span class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${digestDiscord ? "translate-x-5" : ""}`}></span></span>
                            </label>
                            {#if digestDiscord}<div in:slide={{ duration: motionDuration(200) }} out:fade={{ duration: motionDuration(140) }} class="mt-4 space-y-3">
                              <label class="block text-sm font-medium text-surface-800-200">Webhook URL<input class="input mt-2" name="discordWebhook" type="url" placeholder={digestSettings.discord_webhook_set ? "Already configured · leave blank to keep" : "https://discord.com/api/webhooks/…"} /></label>
                              {#if digestSettings.discord_webhook_set}<label class="flex items-center gap-2 text-xs text-surface-700-300"><input class="checkbox checkbox-sm" type="checkbox" name="clearDiscord" /> Clear saved webhook</label>{/if}
                            </div>{:else}<input type="hidden" name="discordWebhook" value="" />{/if}
                          </div>

                          <div class="relative border border-surface-300-700/50 p-4 focus-within:ring-2 focus-within:ring-tertiary-500 focus-within:ring-offset-2 focus-within:ring-offset-surface-950">
                            <input id="digest-email" class="sr-only" type="checkbox" name="channels" value="email" checked={digestEmail} role="switch" aria-label="Send digest by email" onchange={(event) => (digestEmailOverride = (event.currentTarget as HTMLInputElement).checked)} />
                            <label for="digest-email" class="flex cursor-pointer items-center gap-3">
                              <span class="grid size-9 shrink-0 place-items-center preset-tonal-tertiary"><Mail size={17} /></span>
                              <span class="min-w-0 flex-1"><span class="block font-medium text-surface-950-50">Email</span><span class="mt-0.5 block text-xs text-surface-700-300">Send through your SMTP server</span></span>
                              <span class={`relative h-7 w-12 shrink-0 rounded-full p-1 transition ${digestEmail ? "preset-filled-tertiary-500" : "preset-filled-surface-500"}`}><span class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${digestEmail ? "translate-x-5" : ""}`}></span></span>
                            </label>
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
                              {#if digestSettings.smtp_username_set || digestSettings.smtp_password_set}<label class="flex items-center gap-2 text-xs text-surface-700-300"><input class="checkbox checkbox-sm" type="checkbox" name="clearSmtpCredentials" /> Clear saved credentials</label>{/if}
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
                    {@const configured = digestSettings.channels.includes(channel as "discord" | "email")}
                    {#if configured}<form method="POST" action="?/sendDigestTest" use:enhance={setPending(`digest-test-${channel}`)}>
                      <input type="hidden" name="channel" value={channel} />
                      <button class="btn btn-sm min-h-10 preset-tonal-secondary" type="submit" disabled={isPending(`digest-test-${channel}`)} aria-busy={isPending(`digest-test-${channel}`)}>{#if channel === "discord"}<MessageCircle size={15} />{:else}<Mail size={15} />{/if}{#if isPending(`digest-test-${channel}`)}<RefreshCw size={15} class="animate-spin" />{:else}Send {channel} test{/if}</button>
                    </form>{/if}
                  {/each}
                  <form method="POST" action="?/runDigest" use:enhance={setPending("digest-run")} class="ml-auto">
                    <button class="btn btn-sm min-h-10 preset-filled-tertiary-500" type="submit" disabled={!digestSettings.enabled || digestSettings.channels.length === 0 || isPending("digest-run")} aria-busy={isPending("digest-run")}><Send size={15} /> Run digest now</button>
                  </form>
                </div>
                {#if digestSettings.last_delivery}
                  {@const delivery = digestSettings.last_delivery}
                  <div class={`mt-4 flex flex-wrap items-center gap-3 border-l-2 p-3 text-sm ${delivery.status === "failed" ? "border-error-500 preset-tonal-error" : delivery.status === "sent" ? "border-success-500 preset-tonal-success" : "border-secondary-500 preset-tonal-secondary"}`} role="status">
                    {#if delivery.status === "sent"}<Check size={16} class="shrink-0" />{:else if delivery.status === "failed"}<TriangleAlert size={16} class="shrink-0" />{:else}<RefreshCw size={16} class="shrink-0 animate-spin" />{/if}
                    <span class="min-w-0 flex-1">Last {delivery.channel} delivery: <strong>{delivery.status}</strong>{#if delivery.sent_at} · {formatDeliveryDate(delivery.sent_at)}{:else if delivery.updated_at} · {formatDeliveryDate(delivery.updated_at)}{/if}{#if delivery.error}<span class="mt-1 block text-xs">{delivery.error}</span>{/if}</span>
                    {#if delivery.status === "failed"}<form method="POST" action="?/retryDigest" use:enhance={setPending("digest-retry")}><input type="hidden" name="id" value={delivery.id} /><button class="btn btn-sm min-h-9 preset-tonal-secondary" type="submit" disabled={isPending("digest-retry")} aria-busy={isPending("digest-retry")}><RefreshCw size={14} /> Retry</button></form>{/if}
                  </div>
                {/if}
              </section>
              <LlmSettings
                llm={data.llm}
                formState={formState}
                {setPending}
                {isPending}
              />
              <section
                in:fly={{
                  y: 12,
                  duration: motionDuration(300),
                  delay: motionDelay(5),
                }}
                class="card preset-tonal-surface p-5 sm:p-6 lg:col-span-2"
              >
                <div class="mb-5 flex items-center gap-3">
                  <span
                    class="grid size-10 shrink-0 place-items-center preset-tonal-primary"
                    ><BookOpen size={19} /></span
                  >
                  <h2 class="text-lg font-semibold text-surface-950-50">
                    Your reading history
                  </h2>
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
                        {#if item.rating}<span
                            in:scale={{ duration: motionDuration(150) }}
                            class="shrink-0 text-xs font-semibold text-primary-600-400"
                            >{item.rating} ★</span
                          >{/if}
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
    </div>
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
        /><span>{item.shortLabel}</span>{#if activeView === item.id}<span
            in:scale={{ duration: motionDuration(150) }}
            out:fade={{ duration: motionDuration(90) }}
            class="absolute bottom-1 size-1 rounded-full preset-filled-primary-500"
            aria-hidden="true"
          ></span>{/if}{#if item.id === "saved" && savedCount > 0}<span
            in:scale={{ duration: motionDuration(160) }}
            out:fade={{ duration: motionDuration(100) }}
            class="absolute mb-7 ml-6 badge badge-xs preset-filled-primary-500"
            >{savedCount}</span
          >{/if}</button
      >{/each}
  </nav>
  {#if librarrSearchOpen}
    <div
      in:fade={{ duration: motionDuration(180) }}
      out:fade={{ duration: motionDuration(120) }}
      class="fixed inset-0 z-50 grid place-items-center bg-surface-950/70 p-4 backdrop-blur-sm"
      role="presentation"
      onclick={closeLibrarrSearch}
    >
      <dialog
        open
        in:scale={{ duration: motionDuration(220) }}
        out:scale={{ duration: motionDuration(140) }}
        class="relative m-0 card max-h-[min(44rem,calc(100vh-2rem))] w-full max-w-2xl justify-self-center overflow-hidden preset-filled-surface-50-950 shadow-2xl shadow-surface-950/30"
        aria-labelledby="librarr-search-title"
        onclick={(event) => event.stopPropagation()}
        onkeydown={(event) => event.stopPropagation()}
      >
        <div
          class="flex items-start justify-between gap-4 border-b border-surface-200-800 p-5 sm:p-6"
        >
          <div class="min-w-0">
            <p class="text-sm font-medium text-primary-600-400">
              Connected Librarr
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
            class="btn btn-icon btn-sm preset-tonal-surface"
            aria-label="Close Librarr search"
            onclick={closeLibrarrSearch}><X size={17} /></button
          >
        </div>
        <div class="max-h-[calc(100vh-11rem)] overflow-y-auto p-5 sm:p-6">
          <form
            class="flex flex-col gap-3 sm:flex-row sm:items-center"
            onsubmit={(event) => {
              event.preventDefault();
              void searchLibrarr();
            }}
          >
            <label
              class="input flex min-h-12 min-w-0 flex-1 items-center gap-2"
              aria-label="Search Librarr"
              ><Search size={17} class="shrink-0 text-surface-600-400" /><input
                class="input-ghost min-w-0 flex-1 focus:outline-none focus:ring-0"
                bind:value={librarrQuery}
                placeholder="Search title or author"
              /></label
            >
            <label class="sr-only" for="librarr-media-type">Format</label
            ><select
              id="librarr-media-type"
              class="select min-h-12 sm:mx-1 sm:w-40"
              bind:value={librarrMediaType}
              ><option value="audiobook">Audiobook</option><option value="ebook"
                >Ebook</option
              ></select
            >
            <button
              type="submit"
              class="btn min-h-12 preset-filled-primary-500"
              disabled={librarrSearchState === "searching"}
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
              class="mt-4 flex items-start gap-2 preset-tonal-error p-3 text-sm"
              role="alert"
            >
              <CircleHelp size={16} class="mt-0.5 shrink-0" />
              <span>{librarrSearchError}</span>
            </div>{/if}
          {#if librarrSearchState === "searching"}
            <div
              in:fade={{ duration: motionDuration(160) }}
              class="grid min-h-48 place-items-center text-sm text-surface-700-300"
            >
              <RefreshCw size={22} class="animate-spin text-primary-500" /> Searching
              Librarr…
            </div>
          {:else if librarrSearchState === "ready" && librarrResults.length === 0}
            <div
              in:fade={{ duration: motionDuration(160) }}
              class="grid min-h-48 place-items-center text-center text-sm text-surface-700-300"
            >
              <div>
                <BookOpen size={24} class="mx-auto mb-3 text-primary-500" />
                <p>No {librarrMediaType} results found.</p>
                <p class="mt-1">Try a title, author, or a shorter search.</p>
              </div>
            </div>
          {:else if librarrResults.length}
            {#if highConfidenceLibrarrResults.length > 1}<div class="mt-5 flex flex-wrap items-center justify-between gap-3 border border-primary-500/30 preset-tonal-primary p-3 text-sm"><span><strong>{highConfidenceLibrarrResults.length} high-confidence matches</strong><span class="ml-1 text-surface-700-300">(Librarr score ≥ 90)</span></span><button type="button" class="btn btn-sm min-h-9 preset-filled-primary-500" onclick={() => void addHighConfidenceLibrarrResults()} disabled={librarrAddingIndex !== null} aria-busy={librarrAddingIndex === -1}>{#if librarrAddingIndex === -1}<RefreshCw size={14} class="animate-spin" /> Adding…{:else}<Library size={14} /> Add high-confidence matches{/if}</button></div>{/if}
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
                    class="grid size-16 shrink-0 place-items-center overflow-hidden preset-filled-surface-500 sm:size-20"
                  >
                    {#if cover}<img
                        in:fade={{ duration: motionDuration(180) }}
                        class="h-full w-full object-cover"
                        src={cover}
                        alt={`Cover of ${resultTitle(result)}`}
                        loading="lazy"
                        onerror={(event) =>
                          ((event.currentTarget as HTMLImageElement).hidden =
                            true)}
                      />{:else}<BookOpen
                        size={22}
                        class="text-surface-600-400"
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
                  </div>
                  <button
                    type="button"
                    class="btn btn-sm min-h-11 shrink-0 preset-tonal-secondary"
                    disabled={librarrAddingIndex !== null ||
                      librarrAdded.has(index)}
                    aria-busy={librarrAddingIndex === index}
                    onclick={() => void addLibrarrResult(result, index)}
                    >{#if librarrAddingIndex === index}<RefreshCw
                        size={15}
                        class="animate-spin"
                      />{:else if librarrAdded.has(index)}<Check
                        size={15}
                      />{:else}<Library size={15} />{/if}<span
                      class="hidden sm:inline"
                      >{librarrAdded.has(index) ? "Added" : "Add"}</span
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
