export const RATING_VALIDATION_MESSAGE =
  "Rating must be a whole number from 1 to 5, or null for unrated.";
export const HISTORY_ID_VALIDATION_MESSAGE = "Choose a valid reading history entry.";

export type ParsedValue<T> =
  | { ok: true; value: T }
  | { ok: false; message: string };

const hasOwn = (record: object, key: PropertyKey): boolean =>
  Object.prototype.hasOwnProperty.call(record, key);

export function parseWholeStarRating(raw: unknown): ParsedValue<number | null> {
  if (raw === null) {
    return { ok: true, value: null };
  }
  if (raw === undefined || typeof raw === "boolean") {
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
  if (hasOwn(overrides, id)) {
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

export type RatingSurface = "history" | "recent";

export type FocusableElementLike = {
  focus: () => void;
  isConnected?: boolean;
};

export type FocusDocumentLike = {
  activeElement: unknown;
  body?: unknown;
  querySelector: (selector: string) => FocusableElementLike | null;
};

export function preserveRatingFocusAfterUpdate(params: {
  doc: FocusDocumentLike | null | undefined;
  surface: RatingSurface;
  itemId: number;
  hadFocus: boolean;
  clearedViaButton: boolean;
}): void {
  const { doc, surface, itemId, hadFocus, clearedViaButton } = params;
  if (!hadFocus || !doc) return;

  if (clearedViaButton) {
    const firstStar = doc.querySelector(`[data-rating-star="${surface}-${itemId}-1"]`);
    if (firstStar) {
      firstStar.focus();
      return;
    }
  }

  const active = doc.activeElement as FocusableElementLike | null | undefined;
  const lostFocus =
    !active ||
    (doc.body !== undefined && active === doc.body) ||
    active.isConnected === false;

  if (!lostFocus) return;

  const fallback =
    doc.querySelector(`[data-rating-star="${surface}-${itemId}-1"]`) ??
    doc.querySelector(`[data-rating-group^="${surface}-"] [role="radio"][tabindex="0"]`) ??
    doc.querySelector(`[data-rating-star^="${surface}-"][data-rating-star$="-1"]`) ??
    doc.querySelector('[aria-label="Filter read books by rating"]');
  fallback?.focus();
}

export type RatingSaveFn = (id: number, rating: number | null) => Promise<number | null>;

export type RatingControllerState = {
  overrides: Record<number, number | null>;
  committed: Record<number, number | null>;
  pendingIds: Set<number>;
};

export type ReadingHistoryRatingControllerOptions = {
  saveRating: RatingSaveFn;
  onStateChange?: (state: RatingControllerState) => void;
  onItemRatingChange?: (id: number, rating: number | null) => void;
  onCommittedSuccess?: (result: {
    id: number;
    title: string;
    rating: number | null;
  }) => void;
  onRollbackError?: (result: {
    id: number;
    title: string;
    revertedRating: number | null;
    serverMutatedBeforeFailure: boolean;
    error: unknown;
  }) => void;
  afterDomUpdate?: () => Promise<void>;
  doc?: FocusDocumentLike | null;
};

export function createReadingHistoryRatingController(
  options: ReadingHistoryRatingControllerOptions,
) {
  let overrides: Record<number, number | null> = {};
  let committed: Record<number, number | null> = {};
  let pendingIds = new Set<number>();
  const inFlightIds = new Set<number>();
  const inFlightPromises = new Map<number, Promise<void>>();
  const queuedById = new Map<number, { title: string; rating: number | null }>();

  const emitState = () => {
    options.onStateChange?.({
      overrides,
      committed,
      pendingIds,
    });
  };

  function getState(): RatingControllerState {
    return { overrides, committed, pendingIds };
  }

  function reconcileServerHistory(): void {
    if (pendingIds.size === 0) {
      committed = {};
      overrides = {};
      emitState();
      return;
    }
    const nextCommitted: Record<number, number | null> = {};
    const nextOverrides: Record<number, number | null> = {};
    for (const id of pendingIds) {
      if (hasOwn(committed, id)) {
        nextCommitted[id] = committed[id];
      }
      if (hasOwn(overrides, id)) {
        nextOverrides[id] = overrides[id];
      }
    }
    committed = nextCommitted;
    overrides = nextOverrides;
    emitState();
  }

  function update(
    item: { id: number; title: string; rating: number | null },
    nextRating: number | null,
    surface: RatingSurface = "history",
    triggerElement: unknown = null,
    clearedViaButton = false,
  ): Promise<void> {
    const currentRating = effectiveBookRating(item.id, item.rating, overrides);
    if (!inFlightIds.has(item.id) && currentRating === nextRating) {
      return Promise.resolve();
    }
    if (
      inFlightIds.has(item.id) &&
      currentRating === nextRating &&
      (!queuedById.has(item.id) || queuedById.get(item.id)?.rating === nextRating)
    ) {
      return inFlightPromises.get(item.id) ?? Promise.resolve();
    }

    const resolvedDoc =
      options.doc !== undefined
        ? options.doc
        : typeof document !== "undefined"
          ? (document as unknown as FocusDocumentLike)
          : null;
    const hadFocus = Boolean(
      resolvedDoc && triggerElement != null && resolvedDoc.activeElement === triggerElement,
    );

    if (!hasOwn(committed, item.id)) {
      committed = { ...committed, [item.id]: currentRating };
    }

    overrides = { ...overrides, [item.id]: nextRating };
    if (!pendingIds.has(item.id)) {
      pendingIds = new Set([...pendingIds, item.id]);
    }
    emitState();
    options.onItemRatingChange?.(item.id, nextRating);

    if (hadFocus && resolvedDoc) {
      const flush = options.afterDomUpdate ?? (() => Promise.resolve());
      void flush().then(() => {
        preserveRatingFocusAfterUpdate({
          doc: resolvedDoc,
          surface,
          itemId: item.id,
          hadFocus: true,
          clearedViaButton,
        });
      });
    }

    if (inFlightIds.has(item.id)) {
      queuedById.set(item.id, { title: item.title, rating: nextRating });
      return inFlightPromises.get(item.id) ?? Promise.resolve();
    }

    inFlightIds.add(item.id);
    const runPromise = (async () => {
      let targetTitle = item.title;
      let targetRating: number | null = nextRating;
      let mutatedOnServerInRun = false;

      try {
        while (true) {
          try {
            const savedRating = await options.saveRating(item.id, targetRating);
            committed = { ...committed, [item.id]: savedRating };
            mutatedOnServerInRun = true;

            const queued = queuedById.get(item.id);
            if (queued !== undefined) {
              queuedById.delete(item.id);
              if (queued.rating === savedRating) {
                overrides = { ...overrides, [item.id]: savedRating };
                emitState();
                options.onItemRatingChange?.(item.id, savedRating);
                options.onCommittedSuccess?.({
                  id: item.id,
                  title: queued.title,
                  rating: savedRating,
                });
                break;
              }
              targetTitle = queued.title;
              targetRating = queued.rating;
              continue;
            }

            overrides = { ...overrides, [item.id]: savedRating };
            emitState();
            options.onItemRatingChange?.(item.id, savedRating);
            options.onCommittedSuccess?.({
              id: item.id,
              title: targetTitle,
              rating: savedRating,
            });
            break;
          } catch (error) {
            const queued = queuedById.get(item.id);
            if (queued !== undefined) {
              queuedById.delete(item.id);
              targetTitle = queued.title;
              targetRating = queued.rating;
              continue;
            }

            const revertedRating = hasOwn(committed, item.id) ? committed[item.id] : null;
            overrides = { ...overrides, [item.id]: revertedRating };
            emitState();
            options.onItemRatingChange?.(item.id, revertedRating);
            options.onRollbackError?.({
              id: item.id,
              title: targetTitle,
              revertedRating,
              serverMutatedBeforeFailure: mutatedOnServerInRun,
              error,
            });
            break;
          }
        }
      } finally {
        inFlightIds.delete(item.id);
        inFlightPromises.delete(item.id);
        if (pendingIds.has(item.id)) {
          const nextPending = new Set(pendingIds);
          nextPending.delete(item.id);
          pendingIds = nextPending;
        }
        emitState();
      }
    })();

    inFlightPromises.set(item.id, runPromise);
    return runPromise;
  }

  return {
    getState,
    reconcileServerHistory,
    update,
  };
}
