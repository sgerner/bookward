import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parse } from "svelte/compiler";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildBaseBooks,
  commitLocalBookStatus,
  createOptimisticBookStatus,
  createOptimisticImport,
  createPendingSubmit,
  extractActionBookId,
  extractActionBookStatus,
  mergeDiscoverPaginationSlice,
  mergeReconciledBooks,
  reconcileOverviewBooksReload,
  resolveLibrarrDownloadReconciliation,
  type ActionResultLike,
  type DiscoverSessionState,
  type OptimisticChange,
  type ReconcilableBook,
} from "./discover-reconciliation.js";
import { actions, load } from "../routes/+page.server.js";
import { GET as getRecommendationsPage } from "../routes/api/recommendations/+server.js";

type TestBook = ReconcilableBook & {
  title: string;
  author: string;
};

function makeEmptyState(): DiscoverSessionState<TestBook> {
  return {
    activeRunIdOverride: null,
    discoverSessionInitializedOverride: false,
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

function makeRawEngineBook(
  id: number,
  status = "recommended",
  runId?: string,
): Record<string, unknown> {
  return {
    id,
    title: `Book ${id}`,
    author: `Author ${id}`,
    description: `Synopsis ${id}`,
    cover_url: `https://covers.example/${id}.jpg`,
    source_url: `https://books.example/${id}`,
    source_id: 1,
    release_date: "2026-11-15",
    date_kind: "day",
    genres: ["Science Fiction"],
    score: 95 - (id % 20),
    metadata_confidence: 0.9,
    explanation: ["Matches rated books"],
    status,
    normalized_key: `book-${id}::author-${id}`,
    created_at: "2026-10-01 00:00:00",
    updated_at: "2026-10-02 00:00:00",
    isbn13: String(9780000000000 + id),
    isbn10: String(1000000000 + id).slice(0, 10),
    score_batch_id: "batch-1",
    source_name: "Built-in",
    quality_work_id: `/works/OL${id}W`,
    quality_provider: "openlibrary",
    quality_isbn13: String(9780000000000 + id),
    quality_isbn10: String(1000000000 + id).slice(0, 10),
    ...(runId ? { recommendation_run_id: runId } : {}),
  };
}

function collectActionForms(
  node: unknown,
): Array<{ action: string; start: number; end: number }> {
  if (!node || typeof node !== "object") return [];
  const record = node as Record<string, unknown>;
  const found: Array<{ action: string; start: number; end: number }> = [];

  if (record.type === "RegularElement" && record.name === "form") {
    const attributes = record.attributes as Array<{
      type: string;
      name?: string;
      value?: Array<{ data?: string }>;
    }>;
    const actionAttr = attributes.find(
      (attr) => attr.type === "Attribute" && attr.name === "action",
    );
    const action =
      actionAttr?.value?.map((part) => part.data ?? "").join("") ?? "";
    if (action) {
      found.push({
        action,
        start: record.start as number,
        end: record.end as number,
      });
    }
  }

  for (const value of Object.values(record)) {
    if (Array.isArray(value)) {
      for (const child of value) found.push(...collectActionForms(child));
    } else if (value && typeof value === "object") {
      found.push(...collectActionForms(value));
    }
  }
  return found;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("discover reconciliation helpers", () => {
  it("clears optimisticBookStatus overrides on commit by default and restores previous state on rollback", () => {
    let overrides: Record<number, string> = {};
    const deps = {
      hasBook: (id: number) => id === 7,
      getOverrides: () => overrides,
      setOverrides: (next: Record<number, string>) => {
        overrides = next;
      },
    };

    const change = createOptimisticBookStatus(deps, 7, "saved");
    expect(overrides).toEqual({ 7: "saved" });
    change?.commit?.();
    expect(overrides).toEqual({});

    overrides = { 7: "maybe_later" };
    const failedChange = createOptimisticBookStatus(deps, 7, "rejected");
    expect(overrides).toEqual({ 7: "rejected" });
    failedChange?.rollback?.();
    expect(overrides).toEqual({ 7: "maybe_later" });
  });

  it("prevents localOnly createPendingSubmit from triggering refreshAll while preserving standard refresh on non-local actions", async () => {
    const pendingEvents: Array<[string, boolean]> = [];
    const flushTelemetry = vi.fn().mockResolvedValue(undefined);
    const announceError = vi.fn();
    const committedResults: Array<ActionResultLike | undefined> = [];
    let rolledBack = false;

    const localSubmit = createPendingSubmit({
      key: "save-5",
      options: { localOnly: true },
      optimistic: () => ({
        commit: (result) => committedResults.push(result),
        rollback: () => {
          rolledBack = true;
        },
      }),
      setPendingState: (key, pending) => pendingEvents.push([key, pending]),
      flushTelemetry,
      clearOptimisticNotification: () => undefined,
      announceActionError: announceError,
    });

    const localCompletion = await localSubmit({
      formData: new FormData(),
    } as never);
    expect(flushTelemetry).toHaveBeenCalledTimes(1);

    const updateLocal = vi.fn().mockResolvedValue(undefined);
    const successResult: ActionResultLike = {
      type: "success",
      status: 200,
      data: { id: 5, status: "saved" },
    };
    await localCompletion!({
      update: updateLocal,
      result: successResult,
    } as never);

    expect(updateLocal).toHaveBeenCalledWith({
      reset: false,
      refreshAll: false,
      navigate: false,
    });
    expect(committedResults).toEqual([successResult]);
    expect(rolledBack).toBe(false);
    expect(pendingEvents).toEqual([
      ["save-5", true],
      ["save-5", false],
    ]);

    const failingSubmit = createPendingSubmit({
      key: "save-6",
      options: { localOnly: true },
      optimistic: () => ({
        rollback: () => {
          rolledBack = true;
        },
      }),
      setPendingState: () => undefined,
      flushTelemetry: () => Promise.resolve(),
      clearOptimisticNotification: () => undefined,
      announceActionError: announceError,
    });
    const failingCompletion = await failingSubmit({
      formData: new FormData(),
    } as never);
    await failingCompletion!({
      update: vi.fn().mockResolvedValue(undefined),
      result: {
        type: "failure",
        status: 502,
        data: { message: "Engine unavailable", error: true },
      },
    } as never);
    expect(rolledBack).toBe(true);

    const standardSubmit = createPendingSubmit({
      key: "import-5",
      setPendingState: () => undefined,
      flushTelemetry: () => Promise.resolve(),
      clearOptimisticNotification: () => undefined,
      announceActionError: announceError,
    });
    const standardCompletion = await standardSubmit({
      formData: new FormData(),
    } as never);
    const updateStandard = vi.fn().mockResolvedValue(undefined);
    await standardCompletion!({
      update: updateStandard,
      result: { type: "success", status: 200, data: { id: 5, status: "imported" } },
    } as never);
    expect(updateStandard).toHaveBeenCalledWith({
      reset: true,
      navigate: false,
    });
  });

  it("clears client overrides on non-Discover reloads so server truth wins while keeping active Discover slices", () => {
    const initialRecommended: TestBook[] = [
      { id: 1, title: "Book 1", author: "Author 1", status: "recommended", recommendation_run_id: "run-1" },
      { id: 2, title: "Book 2", author: "Author 2", status: "recommended", recommendation_run_id: "run-1" },
    ];
    const paginatedSlice: TestBook[] = [
      { id: 25, title: "Book 25", author: "Author 25", status: "recommended", recommendation_run_id: "run-1" },
    ];
    const state: DiscoverSessionState<TestBook> = {
      ...makeEmptyState(),
      additionalDiscoverBooks: paginatedSlice,
      bookStatusOverrides: { 99: "imported" },
      committedBookStatuses: { 1: "saved", 99: "saved" },
      discoverLoadedCountOverride: 32,
      discoverHasMoreOverride: true,
    };

    // Simulate non-Discover reload (nextRunId === "") where server reports book 99 is actually "imported"
    // (or "saved" before #267) and book 1 is "saved".
    const nextServerBooks: TestBook[] = [
      { id: 1, title: "Book 1", author: "Author 1", status: "saved" },
      { id: 99, title: "Book 99", author: "Author 99", status: "imported" },
      { id: 2, title: "Book 2", author: "Author 2", status: "recommended" },
    ];
    const reconciled = reconcileOverviewBooksReload({
      state,
      previousBooksSnapshot: initialRecommended,
      priorRunId: "run-1",
      nextBooks: nextServerBooks,
      nextDecisions: [],
      nextRunId: "",
    });

    expect(reconciled).not.toBeNull();
    expect(reconciled!.bookStatusOverrides).toEqual({});
    expect(reconciled!.committedBookStatuses).toEqual({});
    expect(reconciled!.activeRunIdOverride).toBe("run-1");
    expect(reconciled!.clientDiscoverBooks?.map((b) => b.id)).toEqual([2]);
    expect(reconciled!.additionalDiscoverBooks.map((b) => b.id)).toEqual([25]);

    const merged = mergeReconciledBooks({
      baseBooks: buildBaseBooks(nextServerBooks, reconciled!.clientDiscoverBooks),
      additionalDiscoverBooks: reconciled!.additionalDiscoverBooks,
      decisions: [],
      additionalDecisionBooks: [],
      sessionBooksById: reconciled!.sessionBooksById,
      committedBookStatuses: reconciled!.committedBookStatuses,
      bookStatusOverrides: reconciled!.bookStatusOverrides,
    });
    expect(merged.find((b) => b.id === 99)?.status).toBe("imported");
    expect(merged.find((b) => b.id === 1)?.status).toBe("saved");
    expect(merged.find((b) => b.id === 2)?.status).toBe("recommended");
    expect(merged.find((b) => b.id === 25)?.status).toBe("recommended");
  });

  it("reconciles undoDecision using the server-returned status rather than client guesswork", () => {
    let state = makeEmptyState();
    const cachedBook: TestBook = {
      id: 42,
      title: "Imported Waitlist Book",
      author: "Author 42",
      status: "imported",
    };

    // Undo response returns the authoritative prior status from decision_events (e.g., "imported" under #267).
    const undoActionResult: ActionResultLike = {
      type: "success",
      status: 200,
      data: {
        message: "Decision undone.",
        id: 42,
        status: "imported",
      },
    };
    const restoredStatus = extractActionBookStatus(undoActionResult);
    const restoredId = extractActionBookId(undoActionResult, 42);
    expect(restoredStatus).toBe("imported");
    expect(restoredId).toBe(42);

    state = {
      ...state,
      ...commitLocalBookStatus(state, cachedBook, restoredId, restoredStatus!),
    };
    const merged = mergeReconciledBooks({
      baseBooks: [],
      additionalDiscoverBooks: [],
      decisions: [],
      additionalDecisionBooks: [],
      sessionBooksById: state.sessionBooksById,
      committedBookStatuses: state.committedBookStatuses,
      bookStatusOverrides: state.bookStatusOverrides,
    });
    expect(merged).toEqual([{ ...cachedBook, status: "imported" }]);
  });

  it("never hard-codes imported on Librarr downloads and invalidates off-Discover views", () => {
    expect(
      resolveLibrarrDownloadReconciliation(
        { candidate: { id: 10, status: "imported" } },
        "saved",
      ),
    ).toEqual({
      candidateStatus: "imported",
      shouldInvalidate: true,
    });

    expect(
      resolveLibrarrDownloadReconciliation(
        { candidate: { id: 10, status: "saved" } },
        "discover",
      ),
    ).toEqual({
      candidateStatus: "saved",
      shouldInvalidate: false,
    });

    expect(
      resolveLibrarrDownloadReconciliation(
        { candidate: { id: 10, status: "imported" } },
        "discover",
      ),
    ).toEqual({
      candidateStatus: "imported",
      shouldInvalidate: false,
    });

    expect(resolveLibrarrDownloadReconciliation({}, "discover")).toEqual({
      candidateStatus: undefined,
      shouldInvalidate: true,
    });
  });

  it("merges paginated Discover slices without duplicating items or losing offset progress", () => {
    const existing: TestBook[] = [
      { id: 1, title: "Book 1", author: "Author 1", status: "saved", recommendation_run_id: "run-1" },
      { id: 2, title: "Book 2", author: "Author 2", status: "recommended", recommendation_run_id: "run-1" },
    ];
    const incoming: TestBook[] = [
      { id: 2, title: "Book 2", author: "Author 2", status: "recommended", recommendation_run_id: "run-1" },
      { id: 25, title: "Book 25", author: "Author 25", status: "recommended", recommendation_run_id: "run-1" },
      { id: 26, title: "Book 26", author: "Author 26", status: "recommended", recommendation_run_id: "run-1" },
    ];

    const merged = mergeDiscoverPaginationSlice({
      existingBooks: existing,
      additionalDiscoverBooks: [],
      incomingItems: incoming,
      hasMore: true,
      responseRunId: "run-1",
      activeRunId: "run-1",
      discoverLoadedCount: 24,
      discoverVisibleCount: 24,
      priorFilteredCount: 23,
      pageSize: 8,
    });

    expect(merged.additionalDiscoverBooks.map((b) => b.id)).toEqual([25, 26]);
    expect(merged.discoverLoadedCountOverride).toBe(27);
    expect(merged.discoverHasMoreOverride).toBe(true);
    expect(merged.discoverVisibleCount).toBe(32);
  });
});

describe("+page.svelte UI form wiring & multi-click Discover session flow", () => {
  it("wires every decide, undoDecision, markRead, and shortlistBulk form in +page.svelte with localOnly enhance", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../routes/+page.svelte", import.meta.url)),
      "utf8",
    );
    const ast = parse(source, { modern: true });
    const forms = collectActionForms(ast.fragment);

    const localOnlyActions = new Set([
      "?/decide",
      "?/undoDecision",
      "?/markRead",
      "?/shortlistBulk",
    ]);
    const matchedLocalForms = forms.filter((form) =>
      localOnlyActions.has(form.action),
    );
    expect(matchedLocalForms.length).toBeGreaterThanOrEqual(8);

    for (const form of matchedLocalForms) {
      const snippet = source.slice(form.start, form.end);
      const usesLocalOnly =
        snippet.includes("use:enhance={setPendingLocal(") ||
        snippet.includes("use:enhance={setPendingDigestBulk(") ||
        (form.action === "?/markRead" &&
          snippet.includes("use:enhance={submitMarkRead("));
      expect(
        usesLocalOnly,
        `Expected ${form.action} form to use setPendingLocal/setPendingDigestBulk/submitMarkRead so update() does not re-run load:\n${snippet.slice(0, 220)}`,
      ).toBe(true);
    }
    expect(source).toMatch(/function submitMarkRead[\s\S]*?localOnly:\s*true/);

    const importForms = forms.filter((form) => form.action === "?/importLibrar");
    expect(importForms.length).toBe(1);
    expect(source.slice(importForms[0].start, importForms[0].end)).toContain(
      "use:enhance={setPending(",
    );

    const refreshForms = forms.filter(
      (form) =>
        form.action === "?/runSync" ||
        form.action === "?/rebuildEmbeddings" ||
        form.action === "?/restoreBackup" ||
        form.action === "?/connectGoodreads" ||
        form.action === "?/importCsv" ||
        form.action === "?/source" ||
        form.action === "?/refreshSource" ||
        form.action === "?/toggleDefault" ||
        form.action === "?/toggleSource",
    );
    expect(refreshForms.length).toBe(10);
    for (const form of refreshForms) {
      expect(source.slice(form.start, form.end)).toContain(
        "refreshDiscoverRun: true",
      );
    }
    expect(source).not.toMatch(/optimisticBookStatus\([^)]*,\s*true\s*\)/);
  });

  it("executes a 5-click Discover triage + pagination + undo + Shortlist Librarr reload flow within 1 recommendation run", async () => {
    const catalog = new Map<number, { status: string; priorStatus: string }>();
    for (let id = 1; id <= 32; id++) {
      catalog.set(id, { status: "recommended", priorStatus: "recommended" });
    }

    let overviewRunCreations = 0;
    let loadCalls = 0;
    let nextDecisionId = 100;
    const decisionsById = new Map<number, { bookId: number; previousStatus: string }>();
    const recordedDecisionBodies: Array<{ id: number; body: Record<string, unknown> }> = [];
    const paginatedRunRequests: string[] = [];

    const baseSettings = {
      embedding_backend: "local",
      embedding_model: "hashing-768",
      embedding_url: "",
      embedding_api_key_set: false,
      librarr_url: "http://librarr.local",
      librarr_api_key_set: true,
      librarr_media_type: "ebook",
      digest: {},
      api_tokens: [],
    };

    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input.toString());

      if (url.pathname === "/api/overview") {
        const trackRun = url.searchParams.get("track_run") !== "false";
        const runId = trackRun ? `run-session-${++overviewRunCreations}` : undefined;
        const recommendedIds = [...catalog.entries()]
          .filter(([, entry]) => entry.status === "recommended")
          .map(([id]) => id)
          .slice(0, 24);
        const nonRecommendedIds = [...catalog.entries()]
          .filter(([, entry]) =>
            ["saved", "imported"].includes(entry.status),
          )
          .map(([id]) => id);
        const decisionIds = [...catalog.entries()]
          .filter(([, entry]) =>
            ["rejected", "maybe_later"].includes(entry.status),
          )
          .map(([id]) => id);

        return Response.json({
          ...(runId ? { recommendation_run_id: runId } : {}),
          recommendations_has_more: true,
          recommendations: [
            ...recommendedIds.map((id) =>
              makeRawEngineBook(id, "recommended", runId),
            ),
            ...nonRecommendedIds.map((id) =>
              makeRawEngineBook(id, catalog.get(id)!.status),
            ),
          ],
          decisions: decisionIds.map((id) =>
            makeRawEngineBook(id, catalog.get(id)!.status),
          ),
          decisions_has_more: false,
          history: [],
          sources: [],
          settings: baseSettings,
        });
      }

      if (url.pathname === "/api/backups") {
        return Response.json({ backups: [] });
      }

      if (url.pathname === "/api/recommendations") {
        const runId = url.searchParams.get("run_id") ?? "";
        const offset = Number(url.searchParams.get("offset") ?? "0");
        const limit = Number(url.searchParams.get("limit") ?? "9");
        paginatedRunRequests.push(`${runId}:${offset}:${limit}`);
        const recommendedIds = [...catalog.entries()]
          .filter(([, entry]) => entry.status === "recommended")
          .map(([id]) => id)
          .slice(offset, offset + limit);
        return Response.json(
          recommendedIds.map((id) => makeRawEngineBook(id, "recommended", runId)),
        );
      }

      const feedbackMatch = url.pathname.match(/^\/api\/recommendations\/(\d+)\/feedback$/);
      if (feedbackMatch) {
        const id = Number(feedbackMatch[1]);
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          action: string;
          run_id?: string;
        };
        recordedDecisionBodies.push({ id, body });
        const previousStatus = catalog.get(id)?.status ?? "recommended";
        const nextStatus =
          body.action === "save"
            ? "saved"
            : body.action === "reject"
              ? "rejected"
              : body.action === "maybe_later"
                ? "maybe_later"
                : "recommended";
        catalog.set(id, { status: nextStatus, priorStatus: previousStatus });
        const decisionId = ++nextDecisionId;
        decisionsById.set(decisionId, { bookId: id, previousStatus });
        return Response.json({
          id,
          status: nextStatus,
          decision_id: decisionId,
        });
      }

      const undoMatch = url.pathname.match(/^\/api\/recommendations\/(\d+)\/undo$/);
      if (undoMatch) {
        const id = Number(undoMatch[1]);
        const body = JSON.parse(String(init?.body ?? "{}")) as { decision_id: number };
        const record = decisionsById.get(body.decision_id);
        const restoredStatus = record?.previousStatus ?? "recommended";
        catalog.set(id, { status: restoredStatus, priorStatus: restoredStatus });
        return Response.json({ id, status: restoredStatus });
      }

      const importMatch = url.pathname.match(/^\/api\/recommendations\/(\d+)\/import$/);
      if (importMatch) {
        const id = Number(importMatch[1]);
        catalog.set(id, { status: "imported", priorStatus: "saved" });
        return Response.json({ id, status: "imported" });
      }

      throw new Error(`Unexpected fetch URL: ${url.toString()}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    // Harness mirroring +page.svelte state + SvelteKit's enhance update() behavior
    let currentViewUrl = new URL("http://afterword.test/");
    let serverData = (await load({
      url: currentViewUrl,
      locals: { user: null },
    } as never)) as {
      recommendation_run_id: string;
      books: TestBook[];
      decisions: TestBook[];
      recommendations_has_more: boolean;
    };
    loadCalls += 1;

    let state = makeEmptyState();
    let previousBooksSnapshot: TestBook[] | null = serverData.books;
    let priorRunId = serverData.recommendation_run_id;
    let discoverVisibleCount = 8;

    const getAllBooks = () =>
      mergeReconciledBooks({
        baseBooks: buildBaseBooks(serverData.books, state.clientDiscoverBooks),
        additionalDiscoverBooks: state.additionalDiscoverBooks,
        decisions: serverData.decisions,
        additionalDecisionBooks: [],
        sessionBooksById: state.sessionBooksById,
        committedBookStatuses: state.committedBookStatuses,
        bookStatusOverrides: state.bookStatusOverrides,
      });

    const getActiveRunId = () =>
      state.activeRunIdOverride ?? serverData.recommendation_run_id;

    const runReload = async (url: URL, isDataRequest = true) => {
      currentViewUrl = url;
      const nextData = (await load({
        url: currentViewUrl,
        isDataRequest,
        locals: { user: null },
      } as never)) as typeof serverData;
      loadCalls += 1;
      const nextState = reconcileOverviewBooksReload({
        state,
        previousBooksSnapshot,
        priorRunId,
        nextBooks: nextData.books,
        nextDecisions: nextData.decisions,
        nextRunId: nextData.recommendation_run_id,
      });
      if (nextState) state = nextState;
      serverData = nextData;
      previousBooksSnapshot = nextData.books;
      priorRunId = getActiveRunId();
    };

    // Simulate SvelteKit's default enhance `update(options)`:
    // In SvelteKit, `refreshAll` defaults to `true` when omitted!
    const simulateEnhanceSubmit = async (params: {
      key: string;
      localOnly: boolean;
      formData: FormData;
      optimistic: (fd: FormData) => OptimisticChange | void;
      serverAction: (fd: FormData) => unknown;
    }) => {
      const submit = createPendingSubmit({
        key: params.key,
        options: params.localOnly ? { localOnly: true } : undefined,
        optimistic: params.optimistic,
        setPendingState: () => undefined,
        flushTelemetry: () => Promise.resolve(),
        clearOptimisticNotification: () => undefined,
        announceActionError: (msg) => {
          throw new Error(msg);
        },
      });
      const complete = await submit({ formData: params.formData } as never);
      const rawActionOutput = await Promise.resolve(
        params.serverAction(params.formData),
      );
      const actionResult: ActionResultLike = {
        type: "success",
        status: 200,
        data: rawActionOutput as Record<string, unknown>,
      };
      await complete!({
        result: actionResult,
        update: async (opts?: {
          reset?: boolean;
          refreshAll?: boolean;
          navigate?: boolean;
        }) => {
          if (opts?.refreshAll !== false) {
            await runReload(currentViewUrl);
          }
        },
      } as never);
      return rawActionOutput as Record<string, unknown>;
    };

    const optimisticDecision = (fd: FormData): OptimisticChange | void => {
      const id = Number(fd.get("id"));
      const nextStatus = String(fd.get("status") ?? "");
      const targetBook = getAllBooks().find((item) => item.id === id);
      if (targetBook) {
        state = {
          ...state,
          sessionBooksById: { ...state.sessionBooksById, [id]: targetBook },
        };
      }
      const change = createOptimisticBookStatus(
        {
          hasBook: (bookId) => getAllBooks().some((item) => item.id === bookId),
          getOverrides: () => state.bookStatusOverrides,
          setOverrides: (next) => {
            state = { ...state, bookStatusOverrides: next };
          },
        },
        id,
        nextStatus,
      );
      return {
        commit: (result) => {
          change?.commit?.(result);
          const committedId = extractActionBookId(result, id);
          const committedStatus = extractActionBookStatus(result) ?? nextStatus;
          state = {
            ...state,
            ...commitLocalBookStatus(
              state,
              getAllBooks().find((item) => item.id === committedId) ??
                state.sessionBooksById[committedId],
              committedId,
              committedStatus,
            ),
          };
        },
        rollback: () => change?.rollback?.(),
      };
    };

    const optimisticUndo = (fd: FormData): OptimisticChange | void => {
      const id = Number(fd.get("id"));
      return {
        commit: (result) => {
          const restoredStatus = extractActionBookStatus(result);
          if (!restoredStatus) return;
          const restoredId = extractActionBookId(result, id);
          state = {
            ...state,
            ...commitLocalBookStatus(
              state,
              getAllBooks().find((item) => item.id === restoredId) ??
                state.sessionBooksById[restoredId],
              restoredId,
              restoredStatus,
            ),
          };
        },
      };
    };

    // Reveal initial 24 cards and paginate past 24 -> 32 cards on run-session-1
    discoverVisibleCount = 24;
    const pageResponse = await getRecommendationsPage({
      url: new URL(
        `http://afterword.test/api/recommendations?status=recommended&limit=8&offset=24&run_id=${getActiveRunId()}`,
      ),
    } as never);
    const pagePayload = (await pageResponse.json()) as {
      items: TestBook[];
      has_more: boolean;
      recommendation_run_id?: string;
    };
    const mergedSlice = mergeDiscoverPaginationSlice({
      existingBooks: getAllBooks(),
      additionalDiscoverBooks: state.additionalDiscoverBooks,
      incomingItems: pagePayload.items,
      hasMore: pagePayload.has_more,
      responseRunId: pagePayload.recommendation_run_id,
      activeRunId: getActiveRunId(),
      discoverLoadedCount: 24,
      discoverVisibleCount,
      priorFilteredCount: 24,
      pageSize: 8,
    });
    state = {
      ...state,
      additionalDiscoverBooks: mergedSlice.additionalDiscoverBooks,
      discoverLoadedCountOverride: mergedSlice.discoverLoadedCountOverride,
      discoverHasMoreOverride: mergedSlice.discoverHasMoreOverride,
    };
    discoverVisibleCount = mergedSlice.discoverVisibleCount;
    expect(paginatedRunRequests).toEqual(["run-session-1:24:9"]);

    // Perform 5 triage clicks across initial and paginated cards
    const fiveClicks: Array<[number, string]> = [
      [1, "saved"],
      [2, "rejected"],
      [3, "maybe_later"],
      [25, "saved"],
      [26, "rejected"],
    ];
    let lastDecisionOutput: Record<string, unknown> = {};
    for (const [bookId, status] of fiveClicks) {
      const fd = new FormData();
      fd.set("id", String(bookId));
      fd.set("status", status);
      fd.set("run_id", getActiveRunId());
      lastDecisionOutput = await simulateEnhanceSubmit({
        key: `decide-${bookId}`,
        localOnly: true,
        formData: fd,
        optimistic: optimisticDecision,
        serverAction: (body) =>
          actions.decide!({
            request: new Request("http://afterword.test", {
              method: "POST",
              body,
            }),
          } as never),
      });
    }

    // All 5 triage clicks ran without re-running `load` or creating new runs!
    expect(overviewRunCreations).toBe(1);
    expect(loadCalls).toBe(1);
    expect(state.bookStatusOverrides).toEqual({});
    expect(
      recordedDecisionBodies.every((entry) => entry.body.run_id === "run-session-1"),
    ).toBe(true);

    // Remaining paginated cards (27..32) stay in Discover!
    const remainingDiscoverIds = getAllBooks()
      .filter((b) => b.status === "recommended")
      .map((b) => b.id);
    expect(remainingDiscoverIds).toContain(27);
    expect(remainingDiscoverIds).toContain(32);
    expect(remainingDiscoverIds).not.toContain(1);
    expect(remainingDiscoverIds).not.toContain(25);
    expect(remainingDiscoverIds).not.toContain(26);

    // Undo the 5th click (book 26) and verify server-returned status ("recommended") restores it
    const undoFd = new FormData();
    undoFd.set("id", "26");
    undoFd.set("decision_id", String(lastDecisionOutput.undo_id));
    await simulateEnhanceSubmit({
      key: `undo-${lastDecisionOutput.undo_id}`,
      localOnly: true,
      formData: undoFd,
      optimistic: optimisticUndo,
      serverAction: (body) =>
        actions.undoDecision!({
          request: new Request("http://afterword.test", {
            method: "POST",
            body,
          }),
        } as never),
    });
    expect(overviewRunCreations).toBe(1);
    expect(loadCalls).toBe(1);
    expect(getAllBooks().find((b) => b.id === 26)?.status).toBe("recommended");

    // Shallow-navigate via pushState to Shortlist (?view=saved) and submit `importLibrar`:
    // Real SvelteKit respond() strips `x-sveltekit-invalidated` from `event.url` (`http://afterword.test/`)
    // and passes `isDataRequest: true` when `update()` runs!
    currentViewUrl = new URL("http://afterword.test/");
    const importFd = new FormData();
    importFd.set("id", "1");
    importFd.set("run_id", "run-session-1");
    await simulateEnhanceSubmit({
      key: "import-1",
      localOnly: false,
      formData: importFd,
      optimistic: (fd) =>
        createOptimisticImport(fd, (committedId, committedStatus) => {
          const book = getAllBooks().find((item) => item.id === committedId);
          const next = commitLocalBookStatus(state, book, committedId, committedStatus);
          state = { ...state, ...next };
        }),
      serverAction: (body) =>
        actions.importLibrar!({
          request: new Request("http://afterword.test/?view=saved", {
            method: "POST",
            body,
          }),
        } as never),
    });

    // Shallow-routed Shortlist reload ran against `/?x-sveltekit-invalidated=01` with track_run=false:
    // STILL only 1 recommendation run total, overrides are clean, server "imported" status is authoritative,
    // and Discover slice 26..32 survives!
    expect(overviewRunCreations).toBe(1);
    expect(loadCalls).toBe(2);
    expect(state.bookStatusOverrides).toEqual({});
    expect(state.committedBookStatuses).toEqual({});
    expect(getAllBooks().find((b) => b.id === 1)?.status).toBe("imported");
    expect(getActiveRunId()).toBe("run-session-1");
    expect(
      getAllBooks()
        .filter((b) => b.status === "recommended")
        .map((b) => b.id),
    ).toContain(32);
  });

  it("derives import status strictly from server response without hard-coding imported and preserves run_id input", () => {
    const pagePath = fileURLToPath(
      new URL("../routes/+page.svelte", import.meta.url),
    );
    const source = readFileSync(pagePath, "utf8");
    const importFormMatch = source.match(
      /<form[^>]*action="\?\/importLibrar"[\s\S]*?<\/form>/,
    );
    expect(importFormMatch?.[0]).toContain('name="run_id"');
    expect(source).not.toContain('optimisticBookStatus(id, "imported")');

    const committed: Array<{ id: number; status: string }> = [];
    const fd = new FormData();
    fd.set("id", "19");
    const change = createOptimisticImport(fd, (id, status) => {
      committed.push({ id, status });
    });
    expect(committed).toEqual([]);

    // When action response carries unified server status (e.g. "read" for an already-read candidate), it uses it.
    change?.commit?.({
      type: "success",
      status: 200,
      data: { id: 19, status: "read" },
    });
    expect(committed).toEqual([{ id: 19, status: "read" }]);

    // When action response omits status and relies on non-localOnly Shortlist load, nothing is fabricated client-side.
    const committedWithoutStatus: Array<{ id: number; status: string }> = [];
    const changeNoStatus = createOptimisticImport(fd, (id, status) => {
      committedWithoutStatus.push({ id, status });
    });
    changeNoStatus?.commit?.({
      type: "success",
      status: 200,
      data: { message: "Book added to the Librarr wishlist." },
    });
    expect(committedWithoutStatus).toEqual([]);
  });

  it("strips locally-read books from preserved Discover slices and resets localReadBookIds/localRecentReads on track_run=false reloads so readCount never double-counts", () => {
    const initialBooks = [
      { id: 1, status: "recommended", recommendation_run_id: "run-read-1" },
      { id: 2, status: "recommended", recommendation_run_id: "run-read-1" },
    ];
    const state: DiscoverSessionState<(typeof initialBooks)[number]> = {
      activeRunIdOverride: "run-read-1",
      discoverSessionInitializedOverride: true,
      clientDiscoverBooks: initialBooks,
      additionalDiscoverBooks: [
        { id: 25, status: "recommended", recommendation_run_id: "run-read-1" },
      ],
      sessionBooksById: {},
      bookStatusOverrides: {},
      committedBookStatuses: { 2: "read" },
      localReadBookIds: new Set([2, 25]),
      localRecentReads: [
        { id: 501, title: "Read Two", author: "Author B", rating: 5 },
      ],
      discoverLoadedCountOverride: 25,
      discoverHasMoreOverride: false,
      discoverLoadError: "",
    };

    const reconciled = reconcileOverviewBooksReload({
      state,
      previousBooksSnapshot: initialBooks,
      priorRunId: "run-read-1",
      nextBooks: [{ id: 1, status: "recommended", recommendation_run_id: "" }],
      nextDecisions: [],
      nextRunId: "",
    });

    expect(reconciled).not.toBeNull();
    expect(reconciled!.clientDiscoverBooks?.map((b) => b.id)).toEqual([1]);
    expect(reconciled!.additionalDiscoverBooks).toEqual([]);
    expect(reconciled!.localReadBookIds.size).toBe(0);
    expect(reconciled!.localRecentReads).toEqual([]);
  });
});
