import { error as httpError, fail as kitFail } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import {
  GOODREADS_CSV_LIMIT_MESSAGE,
  GOODREADS_CSV_MAX_BYTES,
  engine,
  engineStatus,
  isPayloadTooLarge,
} from "#lib/server/engine.js";
import { toPageBook } from "#lib/server/recommendations.js";
import { z } from "zod";

type SourceFilters = {
  include_genres: string[];
  exclude_genres: string[];
};

type BackupStatus = {
  enabled: boolean;
  interval_hours: number;
  retention_count: number;
  recovery_hold: boolean;
  recovery_hold_at?: string | null;
  last_success_at: string | null;
  last_error: string | null;
  next_backup_at: string | null;
  backups: Array<{ id: string; created_at: string; size_bytes: number }>;
  retention_enabled?: boolean;
  evidence_rotation_enabled?: boolean;
  database_size_bytes?: number;
  wal_size_bytes?: number;
  live_size_bytes?: number;
  reclaimable_bytes?: number;
  backup_total_size_bytes?: number;
  auto_vacuum_mode?: "none" | "full" | "incremental";
  evidence_used_bytes?: number;
  evidence_quota_bytes?: number;
  evidence_high_watermark_bytes?: number;
  evidence_low_watermark_bytes?: number;
  evidence_usage_ratio?: number;
  evidence_status?: "healthy" | "warning" | "exceeded";
  evidence_complete_runs?: number;
  evidence_incomplete_runs?: number;
};

type CatalogAuditStatus = {
  status: "idle" | "queued" | "running" | "failed" | "paused" | "complete";
  active: boolean;
  mode: "incremental" | "full";
  last_mode: "incremental" | "full" | null;
  batch_size: number;
  subbatch_size: number;
  remaining: number;
  progress: number;
  started_at: string | null;
  completed_at: string | null;
  job_id: string | null;
  last_error: string | null;
  can_retry: boolean;
};

type CatalogQuality = {
  statuses: Array<{ quality_status: string; count: number }>;
  identifiers: {
    total: number;
    with_isbn13: number;
    with_catalog_match: number;
  };
  flags: Record<string, number>;
  version: string;
  audit: CatalogAuditStatus;
};

type Overview = {
  counts?: { reads?: number };
  recommendation_run_id?: string;
  recommendations: Array<{
    id: number;
    title: string;
    author: string;
    description: string;
    cover_url: string;
    source_url: string;
    release_date: string | null;
    date_kind: string;
    genres: string[];
    score: number;
    metadata_confidence: number;
    explanation: string[];
    status: string;
    source_name: string | null;
  }>;
  recommendations_has_more?: boolean;
  recommendation_genres?: string[];
  decisions?: Overview["recommendations"];
  decisions_has_more?: boolean;
  decision_genres?: string[];
  history: Array<{
    id: number;
    title: string;
    author: string;
    rating: number | null;
    read_at: string | null;
  }>;
  sources: Array<{
    id: number;
    name: string;
    url: string;
    enabled: number;
    kind: string;
    is_default: number;
    is_custom?: boolean;
    lifecycle: "permanent" | "one_time";
    filters: SourceFilters;
    last_status: string | null;
    last_scanned_at: string | null;
    last_attempted_at?: string | null;
  }>;
  settings: {
    embedding_backend: string;
    embedding_model: string;
    embedding_url: string;
    embedding_api_key_set: boolean;
    librarr_url: string;
    librarr_api_key_set: boolean;
    nyt_api_key_set?: boolean;
    goodreads_rss_url?: string;
    goodreads_rss_last_sync_at?: string;
    goodreads_rss_last_status?: string;
    goodreads_rss_last_attempt_at?: string;
    goodreads_rss_last_error?: string;
    librarr_media_type: "ebook" | "audiobook";
    google_books_api_key_set?: boolean;
    source_sync_interval_hours: number;
    digest: DigestSettings;
    llm_rerank?: {
      enabled: boolean;
      provider: string;
      api_url: string;
      model: string;
      api_key_set: boolean;
    };
    api_tokens: Array<{
      id: number;
      name: string;
      token_prefix: string;
      created_at: string;
      last_used_at: string | null;
      revoked_at: string | null;
    }>;
  };
};

type DigestSettings = {
  enabled: boolean;
  channels: Array<"discord" | "email">;
  day: number;
  time: string;
  timezone: string;
  minimum_score: number;
  maximum_books: number;
  only_new: boolean;
  app_url: string;
  discord_webhook_set: boolean;
  email_to: string;
  email_from: string;
  smtp_host: string;
  smtp_port: number;
  smtp_security: "none" | "starttls" | "ssl";
  smtp_username_set: boolean;
  smtp_password_set: boolean;
  last_period: string;
  last_delivery: {
    id: string;
    period_key: string;
    channel: "discord" | "email";
    status: "pending" | "sent" | "failed";
    error: string | null;
    created_at: string;
    updated_at: string;
    sent_at: string | null;
  } | null;
};

const distinctGenres = (items: Array<{ genres: string[] }>) => {
  const seen = new Map<string, string>();
  for (const item of items) {
    for (const raw of item.genres ?? []) {
      const cleaned = String(raw ?? "").trim();
      if (cleaned && !seen.has(cleaned.toLowerCase())) {
        seen.set(cleaned.toLowerCase(), cleaned);
      }
    }
  }
  return [...seen.values()].sort((left, right) => left.localeCompare(right));
};

export const load: PageServerLoad = async ({ url, locals }) => {
  if (locals?.engineUnavailable) {
    throw httpError(503, "Bookward's recommendation engine is unavailable.");
  }
  const fallbackBackupStatus: BackupStatus = {
    enabled: false,
    interval_hours: 0,
    retention_count: 0,
    recovery_hold: false,
    recovery_hold_at: null,
    last_success_at: null,
    last_error: null,
    next_backup_at: null,
    backups: [],
    retention_enabled: false,
    evidence_rotation_enabled: false,
    database_size_bytes: 0,
    wal_size_bytes: 0,
    live_size_bytes: 0,
    reclaimable_bytes: 0,
    backup_total_size_bytes: 0,
    auto_vacuum_mode: "incremental",
    evidence_used_bytes: 0,
    evidence_quota_bytes: 512 * 1024 * 1024,
    evidence_high_watermark_bytes: Math.floor(512 * 1024 * 1024 * 0.8),
    evidence_low_watermark_bytes: Math.floor(512 * 1024 * 1024 * 0.65),
    evidence_usage_ratio: 0,
    evidence_status: "healthy",
    evidence_complete_runs: 0,
    evidence_incomplete_runs: 0,
  };
  const overviewRequest = engine<Overview>(
    "/api/overview?recommendation_limit=24&decision_limit=24",
  );
  const backupStatusRequest = engine<BackupStatus>("/api/backups")
    .then((status) => ({ ...fallbackBackupStatus, ...status }))
    .catch(() => fallbackBackupStatus);
  const catalogQualityRequest = engine<unknown>("/api/candidates/quality").catch(
    () => null,
  );
  let digestPeriod = url.searchParams.get("digest_period");
  const digestReviewRequest = digestPeriod
    ? engine<{
        ids?: unknown[];
        items?: Overview["recommendations"];
        found?: boolean;
      }>(`/api/digest/review?period=${encodeURIComponent(digestPeriod)}`).catch(
        () => null,
      )
    : Promise.resolve(null);
  let overview: Overview;
  try {
    overview = await overviewRequest;
  } catch {
    throw httpError(503, "Bookward's recommendation engine is unavailable.");
  }
  const [rawBackupStatus, rawCatalogQuality] = await Promise.all([
    backupStatusRequest,
    catalogQualityRequest,
  ]);
  const backupStatus: BackupStatus = {
    ...rawBackupStatus,
    recovery_hold: Boolean(rawBackupStatus.recovery_hold),
    recovery_hold_at: rawBackupStatus.recovery_hold_at ?? null,
  };
  const catalogQuality = normalizeCatalogQuality(rawCatalogQuality);
  const builtIn = overview.sources.find((source) => source.is_default);
  const runId = overview.recommendation_run_id ?? "";
  const books = overview.recommendations.map((book) =>
    toPageBook({
      ...book,
      recommendation_run_id: runId,
    }),
  );
  const overviewBooksById = new Map(books.map((book) => [book.id, book]));
  const decisions = (overview.decisions ?? []).map((book) =>
    toPageBook({ ...book, recommendation_run_id: "" }),
  );
  let digestReviewIds: number[] = [];
  let digestReviewBooks: typeof books = [];
  if (digestPeriod) {
    const review = await digestReviewRequest;
    if (review) {
      digestReviewIds = Array.isArray(review.ids)
        ? review.ids.filter((value): value is number => Number.isInteger(value))
        : [];
      if (review.found === false) {
        digestPeriod = null;
        digestReviewIds = [];
      } else if (Array.isArray(review.items)) {
        digestReviewBooks = review.items.map((item) => {
          const existing = overviewBooksById.get(item.id);
          return existing ?? toPageBook({ ...item, recommendation_run_id: "" });
        });
      } else {
        digestReviewBooks = digestReviewIds
          .map((id) => overviewBooksById.get(id))
          .filter((item): item is (typeof books)[number] => Boolean(item && item.status === "recommended"));
      }
    }
  }
  const recommendationGenres = Array.isArray(overview.recommendation_genres)
    ? distinctGenres([{ genres: overview.recommendation_genres }])
    : distinctGenres(books.filter((book) => book.status === "recommended"));
  const decisionGenres = Array.isArray(overview.decision_genres)
    ? distinctGenres([{ genres: overview.decision_genres }])
    : distinctGenres(decisions);
  return {
    user: locals.user,
    recommendation_run_id: runId,
    books,
    recommendations_has_more: overview.recommendations_has_more === true,
    recommendation_genres: recommendationGenres,
    decisions,
    decisions_has_more: overview.decisions_has_more === true,
    decision_genres: decisionGenres,
    history: overview.history,
    readCount: overview.counts?.reads ?? overview.history.length,
    backupStatus,
    catalogQuality,
    digestReview: {
      requested: Boolean(digestPeriod),
      ids: digestReviewIds,
      books: digestReviewBooks,
    },
    sources: overview.sources
      .filter((source) => !source.is_default)
      .map((source) => ({
        ...source,
        label: source.name,
        is_custom: source.is_custom ?? source.kind === "web",
      })),
    profile: {
      goodreads_url: overview.settings.goodreads_rss_url ?? "",
      goodreads_rss_last_sync_at: overview.settings.goodreads_rss_last_sync_at ?? "",
      goodreads_rss_last_status: overview.settings.goodreads_rss_last_status ?? "",
      goodreads_rss_last_attempt_at: overview.settings.goodreads_rss_last_attempt_at ?? "",
      goodreads_rss_last_error: overview.settings.goodreads_rss_last_error ?? "",
      default_source_enabled: builtIn?.enabled ?? 1,
      librar_url: overview.settings.librarr_url,
      librar_connected: overview.settings.librarr_api_key_set ? 1 : 0,
      ...overview.settings,
      source_sync_interval_hours: overview.settings.source_sync_interval_hours ?? 24,
      digest: overview.settings.digest,
      // Keep the UI compatible while an already-running engine is being
      // restarted onto the API-token migration.
      api_tokens: Array.isArray(overview.settings.api_tokens)
        ? overview.settings.api_tokens
        : [],
    },
  };
};

const idSchema = z.coerce.number().int().positive();
const urlSchema = z.url();
const fail = <T extends { message: string }>(statusCode: number, data: T) =>
  kitFail(statusCode, { ...data, error: true as const });
const comparableSourceUrl = (value: string) => {
  try {
    return new URL(value).href;
  } catch {
    return value.trim();
  }
};
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The engine could not complete that request.";
const formText = (data: FormData, key: string) => String(data.get(key) ?? "").trim();
const sourceGenreList = (value: FormDataEntryValue | null) => {
  const seen = new Set<string>();
  return String(value ?? "")
    .split(",")
    .map((item) => item.trim().replace(/\s+/g, " ").slice(0, 80))
    .filter((item) => {
      const key = item.toLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 12);
};
const sourceFiltersFromForm = (data: FormData): SourceFilters => ({
  include_genres: sourceGenreList(data.get("includeGenres")),
  exclude_genres: sourceGenreList(data.get("excludeGenres")),
});
const optionalFormText = (data: FormData, key: string) => {
  const value = formText(data, key);
  return value || undefined;
};
const updateSourceLifecycle = async (
  rawId: FormDataEntryValue | null,
  rawLifecycle: FormDataEntryValue | null,
) => {
  const id = idSchema.safeParse(rawId);
  const lifecycle = z.enum(["permanent", "one_time"]).safeParse(rawLifecycle);
  if (!id.success || !lifecycle.success)
    return fail(400, { message: "Invalid source." });
  try {
    await engine(`/api/sources/${id.data}`, {
      method: "PUT",
      body: JSON.stringify({ lifecycle: lifecycle.data }),
    });
    return {
      message:
        lifecycle.data === "one_time"
          ? "Source changed to a one-time import."
          : "Source restored to a permanent feed.",
    };
  } catch (error) {
    return fail(engineStatus(error), { message: message(error) });
  }
};
async function handleCsvImport(request: Request) {
  let incoming: FormData;
  try {
    incoming = await request.formData();
  } catch (error) {
    if (isPayloadTooLarge(error)) {
      return fail(413, { message: GOODREADS_CSV_LIMIT_MESSAGE });
    }
    throw error;
  }
  const file = incoming.get("file");
  if (!(file instanceof File) || file.size === 0)
    return fail(400, { message: "Choose a Goodreads or StoryGraph CSV export." });
  if (file.size > GOODREADS_CSV_MAX_BYTES)
    return fail(413, { message: GOODREADS_CSV_LIMIT_MESSAGE });
  const body = new FormData();
  body.set("file", file, file.name);
  try {
    const result = await engine<{ imported: number; source?: string }>(
      "/api/import/csv",
      { method: "POST", body },
    );
    const provider =
      result.source === "storygraph_csv" ? "The StoryGraph" : "Goodreads";
    const noun = result.imported === 1 ? "book" : "books";
    return { message: `Imported ${result.imported} ${noun} from ${provider}.` };
  } catch (error) {
    return fail(engineStatus(error), { message: message(error) });
  }
}
export const actions: Actions = {
  createBackup: async ({ request }) => {
    const data = request ? await request.formData().catch(() => new FormData()) : new FormData();
    const confirmClearHold = formText(data, "confirmClearHold") === "CONFIRM";
    try {
      const result = await engine<{ backup: { created_at: string } }>(
        "/api/backups/create",
        {
          method: "POST",
          body: JSON.stringify(
            confirmClearHold ? { confirm_clear_hold: true } : {},
          ),
        },
        10 * 60_000,
      );
      return { message: `Backup created at ${result.backup.created_at}.` };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  dismissRecoveryHold: async ({ request }) => {
    const data = await request.formData();
    if (formText(data, "confirm") !== "KEEP")
      return fail(400, {
        message:
          "Confirm keeping the current database before resuming scheduled backups.",
      });
    try {
      await engine("/api/backups/recovery-hold/dismiss", {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      return {
        message:
          "Recovery hold dismissed. Scheduled backups have resumed for this database.",
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  compactStorage: async () => {
    try {
      const result = await engine<{
        reclaimed_bytes?: number;
        after_bytes?: number;
        migrated_auto_vacuum?: boolean;
      }>("/api/backups/compact", { method: "POST", body: "{}" }, 10 * 60_000);
      const reclaimed = Math.max(0, Number(result.reclaimed_bytes ?? 0));
      const afterBytes = Math.max(0, Number(result.after_bytes ?? 0));
      const formatSize = (bytes: number) =>
        bytes <= 0
          ? "0 KB"
          : bytes < 1024 * 1024
            ? `${Math.max(1, Math.round(bytes / 1024))} KB`
            : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
      const liveSummary = afterBytes > 0 ? ` Live database: ${formatSize(afterBytes)}.` : "";
      return {
        message:
          reclaimed > 0
            ? `Storage compacted; reclaimed ${formatSize(reclaimed)}.${liveSummary}`
            : result.migrated_auto_vacuum
              ? `Incremental vacuum enabled.${liveSummary}`
              : `Database already compact.${liveSummary}`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  setStorageRetention: async ({ request }) => {
    const data = await request.formData();
    const raw = data.get("enabled");
    if (raw !== "1" && raw !== "0" && raw !== "true" && raw !== "false")
      return fail(400, { message: "Choose a valid storage retention setting." });
    const enabled = raw === "1" || raw === "true";
    try {
      await engine("/api/backups/retention", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      return {
        message: enabled
          ? "Automatic storage retention enabled."
          : "Automatic storage retention paused.",
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  setEvidenceRotation: async ({ request }) => {
    const data = await request.formData();
    const raw = data.get("enabled");
    if (raw !== "1" && raw !== "0" && raw !== "true" && raw !== "false")
      return fail(400, { message: "Choose a valid evidence rotation setting." });
    const enabled = raw === "1" || raw === "true";
    try {
      await engine("/api/backups/evidence-rotation", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      return {
        message: enabled
          ? "Replay evidence rotation enabled."
          : "Replay evidence rotation paused.",
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  restoreBackup: async ({ request }) => {
    const data = await request.formData();
    const id = z.string().regex(/^bookward-\d{8}T\d{6}Z-[0-9a-f]{8}$/).safeParse(data.get("backupId"));
    if (!id.success) return fail(400, { message: "Choose a valid backup." });
    if (formText(data, "confirm") !== "RESTORE")
      return fail(400, { message: 'Type RESTORE to confirm replacing the current database.' });
    try {
      await engine(`/api/backups/${encodeURIComponent(id.data)}/restore`, {
        method: "POST",
        body: "{}",
      }, 10 * 60_000);
      return { message: "Database restored. A safety backup of the previous database was created first." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  createApiToken: async ({ request }) => {
    const name = z.string().trim().min(1).max(100).safeParse(
      (await request.formData()).get("name"),
    );
    if (!name.success) return fail(400, { message: "Give the API token a name." });
    try {
      const result = await engine<{ token: string }>("/api/settings/api-tokens", {
        method: "POST",
        body: JSON.stringify({ name: name.data }),
      });
      return {
        message: "API token created. Copy it now; it will not be shown again.",
        token: result.token,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  revokeApiToken: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid API token." });
    try {
      await engine(`/api/settings/api-tokens/${id.data}`, {
        method: "DELETE",
        body: "",
      });
      return { message: "API token revoked." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  decide: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    const runId = z
      .string()
      .trim()
      .max(128)
      .or(z.literal(""))
      .catch("")
      .parse(data.get("run_id"));
    const status = z
      .enum(["saved", "rejected", "maybe_later", "recommended"])
      .safeParse(data.get("status"));
    if (!id.success || !status.success)
      return fail(400, { message: "Invalid recommendation." });
    const action = {
      saved: "save",
      rejected: "reject",
      maybe_later: "maybe_later",
      recommended: "restore",
    }[status.data];
    try {
      const result = await engine<{ decision_id?: number }>(
        `/api/recommendations/${id.data}/feedback`, {
        method: "POST",
        body: JSON.stringify({ action, ...(runId ? { run_id: runId } : {}) }),
      });
      const messages = {
        saved: "Saved to your shortlist.",
        rejected: "Passed on this recommendation.",
        maybe_later: "Set aside for later.",
        recommended: "Returned to Discover.",
      };
      return {
        message: messages[status.data],
        ...(result.decision_id
          ? {
              undo_id: result.decision_id,
              id: id.data,
              undo_label: {
                saved: "shortlisting this book",
                rejected: "passing on this book",
                maybe_later: "setting this book aside",
                recommended: "restoring this book",
              }[status.data],
            }
          : {}),
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  undoDecision: async ({ request }) => {
    const data = await request.formData();
    const rawIds = data.getAll("id");
    const rawDecisionIds = data.getAll("decision_id");
    if (!rawIds.length || rawIds.length !== rawDecisionIds.length)
      return fail(400, { message: "This decision can no longer be undone." });
    const seen = new Set<string>();
    const decisions: Array<{ id: number; decision_id: number }> = [];
    for (let index = 0; index < rawIds.length; index += 1) {
      const id = idSchema.safeParse(rawIds[index]);
      const decisionId = idSchema.safeParse(rawDecisionIds[index]);
      if (!id.success || !decisionId.success)
        return fail(400, { message: "This decision can no longer be undone." });
      const key = `${id.data}:${decisionId.data}`;
      if (seen.has(key)) continue;
      seen.add(key);
      decisions.push({ id: id.data, decision_id: decisionId.data });
    }
    try {
      if (decisions.length === 1) {
        await engine(`/api/recommendations/${decisions[0].id}/undo`, {
          method: "POST",
          body: JSON.stringify({ decision_id: decisions[0].decision_id }),
        });
        return { message: "Decision undone." };
      }
      await engine("/api/recommendations/bulk-undo", {
        method: "POST",
        body: JSON.stringify({ decisions }),
      });
      return { message: "Decisions undone." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  markRead: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    const rawRating = formText(data, "rating");
    const rating = rawRating ? Number(rawRating) : undefined;
    if (
      !id.success ||
      (rating !== undefined && (!Number.isInteger(rating) || rating < 1 || rating > 5))
    ) {
      return fail(400, { message: "Choose a valid book and an optional rating from 1 to 5." });
    }
    try {
      await engine(`/api/recommendations/${id.data}/read`, {
        method: "POST",
        body: JSON.stringify({
          ...(rating === undefined ? {} : { rating }),
        }),
      });
      return {
        message: rating === undefined
          ? "Marked Read and added to your history."
          : `Marked Read and added to your history with a ${rating}-star rating.`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  source: async ({ request }) => {
    const data = await request.formData();
    const url = urlSchema.safeParse(data.get("url"));
    const label = z.preprocess(
      (value) =>
        value === null || (typeof value === "string" && !value.trim())
          ? undefined
          : value,
      z.string().trim().min(2).max(100).optional(),
    ).safeParse(data.get("label"));
    const lifecycle = z
      .enum(["permanent", "one_time"])
      .catch("permanent")
      .parse(data.get("lifecycle"));
    const filters = sourceFiltersFromForm(data);
    if (!url.success || !label.success)
      return fail(400, { message: "Add a valid name and public URL." });
    const name = label.data ?? new URL(url.data).hostname.replace(/^www\./i, "");
    try {
      const preview = await engine<{ count: number; url?: string }>("/api/sources/preview", {
        method: "POST",
        body: JSON.stringify({ url: url.data, filters }),
      });
      if (preview.count === 0)
        return fail(400, {
          message: filters.include_genres.length || filters.exclude_genres.length
            ? "No books matched these filters."
            : "No recognizable books were found at that URL.",
        });
      const resolvedUrl =
        typeof preview.url === "string" && preview.url.trim()
          ? preview.url.trim()
          : url.data;
      await engine("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          name,
          url: resolvedUrl,
          lifecycle,
          filters,
        }),
      });
      return {
        message: `${lifecycle === "one_time" ? "One-time import" : "Permanent source"} added; its first scan is queued (${preview.count} book${preview.count === 1 ? "" : "s"} detected).`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  editSource: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    const rawLabel = String(data.get("label") ?? "");
    const rawUrl = String(data.get("url") ?? "");
    if (!id.success) return fail(400, { message: "Invalid source." });
    const editState = { id: id.data, label: rawLabel, url: rawUrl };
    const label = z.string().trim().min(2).max(100).safeParse(rawLabel);
    const url = urlSchema.safeParse(rawUrl.trim());
    if (!label.success || !url.success)
      return fail(400, {
        message: "Add a valid name and public URL.",
        editSource: editState,
      });
    try {
      const sources = await engine<Overview["sources"]>("/api/sources");
      const current = sources.find((item) => item.id === id.data);
      if (!current)
        return fail(404, {
          message: "Source not found.",
          editSource: editState,
        });
      if (
        current.kind !== "web" ||
        current.is_default ||
        current.is_custom === false
      )
        return fail(400, {
          message: "Only custom sources can be edited.",
          editSource: editState,
        });
      const nextComparable = comparableSourceUrl(url.data);
      const urlChanged = nextComparable !== comparableSourceUrl(current.url);
      if (urlChanged) {
        if (
          sources.some(
            (item) =>
              item.id !== id.data &&
              comparableSourceUrl(item.url) === nextComparable,
          )
        )
          return fail(409, {
            message: "A source with that URL already exists.",
            editSource: editState,
          });
        const filters = current.filters ?? {
          include_genres: [],
          exclude_genres: [],
        };
        const preview = await engine<{ count: number; url?: string }>(
          "/api/sources/preview",
          {
            method: "POST",
            body: JSON.stringify({ url: url.data, filters }),
          },
        );
        if (preview.count === 0)
          return fail(400, {
            message:
              filters.include_genres.length || filters.exclude_genres.length
                ? "No books matched these filters."
                : "No recognizable books were found at that URL.",
            editSource: editState,
          });
        const resolvedUrl =
          typeof preview.url === "string" && preview.url.trim()
            ? preview.url.trim()
            : url.data;
        const resolvedComparable = comparableSourceUrl(resolvedUrl);
        if (
          sources.some(
            (item) =>
              item.id !== id.data &&
              comparableSourceUrl(item.url) === resolvedComparable,
          )
        )
          return fail(409, {
            message: "A source with that URL already exists.",
            editSource: editState,
          });
        await engine(`/api/sources/${id.data}`, {
          method: "PUT",
          body: JSON.stringify({ name: label.data, url: resolvedUrl }),
        });
        const resolvedUrlChanged =
          resolvedComparable !== comparableSourceUrl(current.url);
        const provenanceNote = resolvedUrlChanged
          ? " Earlier books stay attributed to this source."
          : "";
        return {
          message:
            current.enabled && resolvedUrlChanged
              ? `Source updated; a fresh scan is queued (${preview.count} book${preview.count === 1 ? "" : "s"} detected).${provenanceNote}`
              : `Source updated (${preview.count} book${preview.count === 1 ? "" : "s"} detected).${provenanceNote}`,
        };
      }
      await engine(`/api/sources/${id.data}`, {
        method: "PUT",
        body: JSON.stringify({ name: label.data }),
      });
      return { message: "Source updated." };
    } catch (error) {
      return fail(engineStatus(error), {
        message: message(error),
        editSource: editState,
      });
    }
  },
  configureSourceFilters: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    if (!id.success) return fail(400, { message: "Invalid source." });
    const filters = sourceFiltersFromForm(data);
    const includeGenresRaw = formText(data, "includeGenres");
    const excludeGenresRaw = formText(data, "excludeGenres");
    const submittedUrlRaw = formText(data, "url");
    const currentUrlRaw = formText(data, "currentUrl");
    const intent = formText(data, "intent") || "save";
    const confirmZeroMatch = formText(data, "confirmZeroMatch") === "1";

    let targetUrl = "";
    let skipLivePreview = false;
    if (submittedUrlRaw) {
      const parsedUrl = urlSchema.safeParse(submittedUrlRaw);
      if (!parsedUrl.success) {
        return fail(400, { message: "Enter a valid source URL." });
      }
      targetUrl = parsedUrl.data;
    }

    try {
      if (!targetUrl) {
        const overview = await engine<Overview>(
          "/api/overview?recommendation_limit=1&decision_limit=1",
        );
        const existing = overview.sources.find((item) => item.id === id.data);
        if (!existing) return fail(404, { message: "Source not found." });
        if (existing.kind === "association" || existing.kind === "builtin") {
          skipLivePreview = true;
        } else {
          targetUrl = existing.url;
        }
      }

      let previewCount: number | null = null;
      let previewRawCount: number | null = null;
      let resolvedTargetUrl = targetUrl;
      if (!skipLivePreview) {
        const preview = await engine<{
          count: number;
          raw_count?: number;
          url?: string;
        }>(
          "/api/sources/preview",
          {
            method: "POST",
            body: JSON.stringify({
              url: targetUrl,
              filters,
            }),
          },
        );
        if (typeof preview.url === "string" && preview.url.trim()) {
          resolvedTargetUrl = preview.url.trim();
        }
        previewCount = preview.count;
        previewRawCount =
          typeof preview.raw_count === "number"
            ? preview.raw_count
            : preview.count;

        if (intent === "preview") {
          const messageText =
            previewCount === 0
              ? previewRawCount > 0
                ? `Preview found ${previewRawCount} book${previewRawCount === 1 ? "" : "s"}, but 0 matched these filters.`
                : "Preview found 0 recognizable books at that URL."
              : `Preview matched ${previewCount} of ${previewRawCount} book${previewRawCount === 1 ? "" : "s"}.`;
          return {
            message: messageText,
            sourcePreview: {
              id: id.data,
              url: resolvedTargetUrl,
              count: previewCount,
              rawCount: previewRawCount,
              includeGenres: includeGenresRaw,
              excludeGenres: excludeGenresRaw,
            },
          };
        }

        const urlChanged = Boolean(
          submittedUrlRaw && (!currentUrlRaw || submittedUrlRaw !== currentUrlRaw),
        );
        if (previewRawCount === 0 && urlChanged) {
          return fail(400, {
            message: "No recognizable books were found at that URL.",
          });
        }

        if (previewCount === 0 && !confirmZeroMatch) {
          const hasFilters = Boolean(
            filters.include_genres.length || filters.exclude_genres.length,
          );
          const warningMessage =
            previewRawCount > 0 && hasFilters
              ? `0 of ${previewRawCount} book${previewRawCount === 1 ? "" : "s"} in this feed matched these filters. Confirm to save anyway (existing candidates will be kept).`
              : "No books matched this source configuration. Confirm to save anyway (existing candidates will be kept).";
          return fail(400, {
            message: warningMessage,
            zeroMatchConfirm: {
              id: id.data,
              url: submittedUrlRaw || resolvedTargetUrl,
              rawCount: previewRawCount,
              includeGenres: includeGenresRaw,
              excludeGenres: excludeGenresRaw,
            },
          });
        }
      }

      const updatePayload: {
        url?: string;
        filters: SourceFilters;
      } = { filters };
      if (submittedUrlRaw && (!currentUrlRaw || submittedUrlRaw !== currentUrlRaw)) {
        updatePayload.url = resolvedTargetUrl;
      }
      const updated = await engine<{ job_id?: string | number | null }>(
        `/api/sources/${id.data}`,
        {
          method: "PUT",
          body: JSON.stringify(updatePayload),
        },
      );
      const scanSuffix =
        !("job_id" in updated) || Boolean(updated.job_id)
          ? "; a fresh scan is queued."
          : ".";
      if (updatePayload.url) {
        return {
          message:
            previewCount === null
              ? `Source URL and filters updated${scanSuffix}`
              : `Source updated (${previewCount} book${previewCount === 1 ? "" : "s"} matched)${scanSuffix}`,
        };
      }
      if (previewCount === 0) {
        return {
          message: `Zero-match source filters confirmed and saved${scanSuffix}`,
        };
      }
      return {
        message:
          previewCount === null
            ? `Source filters updated${scanSuffix}`
            : `Source filters updated (${previewCount} book${previewCount === 1 ? "" : "s"} matched)${scanSuffix}`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  switchSourceLifecycle: async ({ request }) => {
    const data = await request.formData();
    return updateSourceLifecycle(data.get("id"), data.get("lifecycle"));
  },
  switchSourceToOneTime: async ({ request }) =>
    updateSourceLifecycle((await request.formData()).get("id"), "one_time"),
  refreshSource: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid source." });
    try {
      await engine(`/api/sources/${id.data}/refresh`, {
        method: "POST",
        body: "{}",
      });
      return { message: "Source refresh queued." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  switchSourceToPermanent: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid source." });
    try {
      const updated = await engine<{ job_id?: string | number | null }>(
        `/api/sources/${id.data}`,
        {
          method: "PUT",
          body: JSON.stringify({ lifecycle: "permanent" }),
        },
      );
      const scanSuffix =
        !("job_id" in updated) || Boolean(updated.job_id)
          ? "; a fresh scan is queued."
          : ".";
      return { message: `Source changed to a permanent feed${scanSuffix}` };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  toggleSource: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid source." });
    try {
      await engine(`/api/sources/${id.data}/toggle`, {
        method: "PUT",
        body: "{}",
      });
      return { message: "Source preference updated." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  configureSourceSchedule: async ({ request }) => {
    const interval = z
      .coerce
      .number()
      .int()
      .min(0)
      .max(720)
      .safeParse((await request.formData()).get("intervalHours"));
    if (!interval.success)
      return fail(400, { message: "Choose a valid source refresh cadence." });
    try {
      await engine("/api/sources/schedule", {
        method: "PUT",
        body: JSON.stringify({ interval_hours: interval.data }),
      });
      if (interval.data === 0)
        return { message: "Automatic permanent-source scanning paused." };
      const label = interval.data === 168
        ? "weekly"
        : interval.data === 24
          ? "daily (UTC)"
          : `every ${interval.data} hours`;
      return { message: `Permanent sources will be scanned ${label}.` };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  toggleDefault: async () => {
    try {
      await engine("/api/sources/default/toggle", {
        method: "PUT",
        body: "{}",
      });
      return { message: "Upcoming-book source updated." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  connectGoodreads: async ({ request }) => {
    const formData = await request.formData();
    const rawUrl = formData.get("url");
    if (typeof rawUrl !== "string")
      return fail(400, { message: "Enter a valid Goodreads RSS URL." });
    const value = rawUrl.trim();
    if (!value) {
      try {
        await engine("/api/settings/goodreads-rss", {
          method: "PUT",
          body: JSON.stringify({ url: "" }),
        });
        return { message: "Goodreads RSS feed removed; nightly sync is off." };
      } catch (error) {
        return fail(engineStatus(error), { message: message(error) });
      }
    }
    const url = urlSchema.safeParse(value);
    if (!url.success)
      return fail(400, { message: "Enter a valid Goodreads RSS URL." });
    try {
      await engine("/api/settings/goodreads-rss", {
        method: "PUT",
        body: JSON.stringify({ url: url.data }),
      });
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
    try {
      const result = await engine<{ imported: number }>(
        "/api/import/goodreads/rss",
        { method: "POST", body: JSON.stringify({ url: url.data }) },
      );
      return {
        message: `Saved and synced ${result.imported} books from Goodreads. Automatic sync runs nightly at 02:00 UTC.`,
      };
    } catch (error) {
      return fail(502, {
        message: `The feed was saved for nightly sync, but this sync failed: ${message(error)}`,
      });
    }
  },
  disconnectGoodreads: async () => {
    try {
      await engine("/api/settings/goodreads-rss", {
        method: "PUT",
        body: JSON.stringify({ url: "" }),
      });
      return { message: "Goodreads RSS feed removed; nightly sync is off." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  importCsv: async ({ request }) => {
    return handleCsvImport(request);
  },
  importGoodreadsCsv: async ({ request }) => {
    return handleCsvImport(request);
  },
  configureLibrar: async ({ request }) => {
    const data = await request.formData();
    const url = urlSchema.safeParse(data.get("url"));
    const key = z
      .string()
      .trim()
      .min(4)
      .or(z.literal(""))
      .safeParse(data.get("apiKey"));
    const mediaType = z
      .enum(["ebook", "audiobook"])
      .safeParse(data.get("mediaType"));
    if (!url.success || !key.success || !mediaType.success)
      return fail(400, {
        message: "Enter a valid Librarr URL, API key, and media type.",
      });
    try {
      await engine("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          librarr_url: url.data,
          librarr_api_key: key.data,
          librarr_media_type: mediaType.data,
        }),
      });
      return {
        message: `Librarr connection saved for ${mediaType.data === "ebook" ? "ebooks" : "audiobooks"}.`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  importLibrar: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    const runId = z
      .string()
      .trim()
      .max(128)
      .or(z.literal(""))
      .catch("")
      .parse(data.get("run_id"));
    if (!id.success) return fail(400, { message: "Invalid book." });
    try {
      await engine(`/api/recommendations/${id.data}/import`, {
        method: "POST",
        body: JSON.stringify(runId ? { run_id: runId } : {}),
      });
      return { message: "Book added to the Librarr wishlist." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  configureEmbeddings: async ({ request }) => {
    const data = await request.formData();
    const backend = z
      .enum(["local", "fastembed", "ollama", "openai-compatible"])
      .safeParse(data.get("backend"));
    const model = z.string().trim().min(2).safeParse(data.get("model"));
    if (!backend.success || !model.success)
      return fail(400, { message: "Choose an embedding backend and model." });
    try {
      await engine("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          embedding_backend: backend.data,
          embedding_model: model.data,
          embedding_url: String(data.get("url") || ""),
          embedding_api_key: String(data.get("apiKey") || ""),
        }),
      });
      return { message: "Embedding provider saved and checked." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  configureGoogleBooks: async ({ request }) => {
    const data = await request.formData();
    const apiKey = String(data.get("apiKey") || "").trim();
    const clear = data.get("clear") === "on";
    try {
      const result = await engine<{
        saved: boolean;
        google_books_api_key_set: boolean;
        job_ids?: Record<string, string | number | null>;
        validation_status?: "verified" | "deferred";
        validation_warning?: string;
      }>("/api/settings/google-books", {
        method: "PUT",
        body: JSON.stringify({
          api_key: apiKey || null,
          clear,
        }),
      });
      if (!clear && !apiKey && !result.google_books_api_key_set) {
        return fail(400, { message: "Enter a Google Books API key." });
      }
      if (clear) {
        return { message: "Google Books API key cleared." };
      }
      if (!apiKey) {
        return { message: "Existing Google Books API key kept." };
      }
      if (result.validation_status === "deferred") {
        return {
          message: result.validation_warning
            ? `${result.validation_warning} Metadata refresh queued.`
            : "Google Books API key saved (live verification deferred); metadata refresh queued.",
        };
      }
      return {
        message: "Google Books API key verified and saved; metadata refresh queued.",
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  configureLlmRerank: async ({ request }) => {
    const data = await request.formData();
    const enabled = data.get("enabled") === "on";
    const provider = String(data.get("provider") || "").trim();
    const apiUrl = String(data.get("apiUrl") || "").trim();
    const model = String(data.get("model") || "").trim();
    const apiKey = String(data.get("apiKey") || "").trim();
    const clearApiKey = data.get("clearApiKey") === "on";
    if (enabled && (!apiUrl || !model)) {
      return fail(400, {
        message: "Select or enter both an LLM API URL and model.",
      });
    }
    try {
      await engine("/api/settings/llm-rerank", {
        method: "PUT",
        body: JSON.stringify({
          enabled,
          provider,
          api_url: apiUrl,
          model,
          api_key: apiKey || null,
          clear_api_key: clearApiKey,
        }),
      });
      return { message: "LLM reranker settings saved." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  rebuildEmbeddings: async () => {
    try {
      const result = await engine<{ job_id: string }>(
        "/api/embeddings/rebuild",
        { method: "POST", body: "{}" },
      );
      const job = await waitForJob(result.job_id, 20);
      if (job.status === "complete")
        return {
          message: "Embeddings rebuilt and recommendations refreshed.",
        };
      if (job.status === "failed")
        return fail(502, {
          message: job.error || "Embedding rebuild failed.",
        });
      return {
        message: `Embedding rebuild is still running (${result.job_id.slice(0, 8)}).`,
      };
    } catch (error) {
      return fail(502, { message: message(error) });
    }
  },
  runSync: async () => {
    try {
      const result = await engine<{ job_id: string }>("/api/sync", {
        method: "POST",
        body: "{}",
      });
      const job = await waitForJob(result.job_id, 20);
      if (job.status === "complete") {
        try {
          const parsed = job.result ? JSON.parse(job.result) as { errors?: unknown[] } : {};
          if (Array.isArray(parsed.errors) && parsed.errors.length)
            return { message: `Sources refreshed with ${parsed.errors.length} source${parsed.errors.length === 1 ? "" : "s"} needing attention.` };
        } catch {
          // A malformed optional result should not turn a completed sync into
          // a failed UI action.
        }
        return { message: "Sources scanned and recommendations refreshed." };
      }
      if (job.status === "failed")
        return fail(502, {
          message: job.error || "Recommendation refresh failed.",
        });
      return {
        message: `Refresh is still running (${result.job_id.slice(0, 8)}).`,
      };
    } catch (error) {
      return fail(502, { message: message(error) });
    }
  },
  auditCatalog: async ({ request }) => {
    const data = await request.formData();
    const rawMode = formText(data, "mode") || "incremental";
    const parsedMode = z.enum(["incremental", "full"]).safeParse(rawMode);
    if (!parsedMode.success)
      return fail(400, { message: "Choose a valid catalog audit mode." });
    const mode = parsedMode.data;
    const modeLabel =
      mode === "full" ? "Full catalog audit" : "Incremental catalog audit";
    try {
      const queued = await engine<{
        job_id: string;
        mode?: string;
        remaining?: number;
      }>("/api/candidates/audit", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      const job = await waitForJob(queued.job_id, 2, 250);
      if (job.status === "failed")
        return fail(502, {
          message: job.error || `${modeLabel} failed.`,
        });
      if (job.status === "complete") {
        const remaining = parseAuditRemaining(job.result);
        if (remaining !== null && remaining > 0)
          return {
            message: `${modeLabel} batch finished; ${remaining} book${remaining === 1 ? "" : "s"} remaining in the background.`,
          };
        return {
          message: `${modeLabel} complete and recommendations queued for refresh.`,
        };
      }
      const remainingSuffix =
        typeof queued.remaining === "number" && queued.remaining > 0
          ? ` (${queued.remaining} book${queued.remaining === 1 ? "" : "s"} remaining)`
          : "";
      return {
        message: `${modeLabel} queued in bounded 50-book batches${remainingSuffix}.`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  retryCatalogAudit: async () => {
    try {
      const queued = await engine<{
        job_id: string;
        mode?: string;
        remaining?: number;
      }>("/api/candidates/audit", {
        method: "POST",
        body: JSON.stringify({ retry: true }),
      });
      const job = await waitForJob(queued.job_id, 2, 250);
      if (job.status === "failed")
        return fail(502, {
          message: job.error || "Catalog audit retry failed.",
        });
      if (job.status === "complete") {
        const remaining = parseAuditRemaining(job.result);
        if (remaining !== null && remaining > 0)
          return {
            message: `Catalog audit retry batch finished; ${remaining} book${remaining === 1 ? "" : "s"} remaining in the background.`,
          };
        return {
          message: "Catalog audit complete and recommendations queued for refresh.",
        };
      }
      return {
        message: `Catalog audit retry queued (${queued.job_id.slice(0, 8)}).`,
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  configureDigest: async ({ request }) => {
    const data = await request.formData();
    const enabled = data.get("enabled") === "on";
    const channels = data
      .getAll("channels")
      .filter((value): value is "discord" | "email" =>
        value === "discord" || value === "email",
      );
    const day = z.coerce.number().int().min(1).max(7).safeParse(data.get("day"));
    const time = z
      .string()
      .regex(/^(?:[01]\d|2[0-3]):[0-5]\d$/)
      .safeParse(data.get("time"));
    const timezone = z.string().trim().min(1).max(80).safeParse(data.get("timezone"));
    const minimumScore = z.coerce
      .number()
      .min(0)
      .max(100)
      .safeParse(data.get("minimumScore"));
    const maximumBooks = z.coerce
      .number()
      .int()
      .min(1)
      .max(20)
      .safeParse(data.get("maximumBooks"));
    const appUrl = urlSchema.safeParse(data.get("appUrl"));
    const smtpPort = z.coerce
      .number()
      .int()
      .min(1)
      .max(65535)
      .safeParse(data.get("smtpPort"));
    const smtpSecurity = z
      .enum(["none", "starttls", "ssl"])
      .safeParse(data.get("smtpSecurity"));
    if (
      !day.success ||
      !time.success ||
      !timezone.success ||
      !minimumScore.success ||
      !maximumBooks.success ||
      !appUrl.success ||
      !smtpPort.success ||
      !smtpSecurity.success
    )
      return fail(400, { message: "Choose valid digest timing and delivery settings." });

    const webhook = String(data.get("discordWebhook") || "").trim();
    const emailTo = String(data.get("emailTo") || "").trim();
    const emailFrom = String(data.get("emailFrom") || "").trim();
    const smtpHost = String(data.get("smtpHost") || "").trim();
    const smtpUsername = String(data.get("smtpUsername") || "").trim();
    const smtpPassword = String(data.get("smtpPassword") || "");
    const payload: Record<string, unknown> = {
      enabled,
      channels,
      day: day.data,
      time: time.data,
      timezone: timezone.data,
      minimum_score: minimumScore.data,
      maximum_books: maximumBooks.data,
      only_new: data.get("onlyNew") === "on",
      app_url: appUrl.data,
      email_to: emailTo,
      email_from: emailFrom,
      smtp_host: smtpHost,
      smtp_port: smtpPort.data,
      smtp_security: smtpSecurity.data,
    };
    // Secret inputs are optional on purpose: an empty password/webhook should
    // keep the encrypted value already stored by the engine. Explicit clear
    // controls provide a recoverable way to remove one.
    if (data.get("clearDiscord") === "on") payload.discord_webhook_url = "";
    else if (webhook) payload.discord_webhook_url = webhook;
    if (data.get("clearSmtpCredentials") === "on") {
      payload.smtp_username = "";
      payload.smtp_password = "";
    } else {
      if (smtpUsername) payload.smtp_username = smtpUsername;
      if (smtpPassword) payload.smtp_password = smtpPassword;
    }
    try {
      await engine("/api/digest/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      return { message: "Weekly digest settings saved." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  sendDigestTest: async ({ request }) => {
    const channel = z.enum(["discord", "email"]).safeParse(
      (await request.formData()).get("channel"),
    );
    if (!channel.success) return fail(400, { message: "Choose a digest channel to test." });
    try {
      const queued = await engine<{ job_id: string }>("/api/digest/test", {
        method: "POST",
        body: JSON.stringify({ channel: channel.data }),
      });
      const job = await waitForJob(queued.job_id);
      if (job.status === "failed")
        return fail(502, { message: job.error || "Digest test failed." });
      const deliveryError = digestJobFailure(job.result);
      if (deliveryError) return fail(502, { message: deliveryError });
      if (job.status !== "complete")
        return { message: `${channel.data === "discord" ? "Discord" : "Email"} test is still running (${queued.job_id.slice(0, 8)}).` };
      return { message: `${channel.data === "discord" ? "Discord" : "Email"} test sent.` };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  runDigest: async () => {
    try {
      const queued = await engine<{ job_id: string }>("/api/digest/run", {
        method: "POST",
        body: "{}",
      });
      const job = await waitForJob(queued.job_id);
      if (job.status === "failed")
        return fail(502, { message: job.error || "Digest could not be delivered." });
      const deliveryError = digestJobFailure(job.result);
      if (deliveryError) return fail(502, { message: deliveryError });
      return { message: digestRunMessage(job.result, queued.job_id, job.status) };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  retryDigest: async ({ request }) => {
    const id = z.uuid().safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid delivery." });
    try {
      const queued = await engine<{ job_id: string }>(
        `/api/digest/deliveries/${id.data}/retry`,
        { method: "POST", body: "{}" },
      );
      const job = await waitForJob(queued.job_id);
      if (job.status === "failed")
        return fail(502, { message: job.error || "Digest retry failed." });
      const deliveryError = digestJobFailure(job.result);
      if (deliveryError) return fail(502, { message: deliveryError });
      if (job.status !== "complete")
        return { message: `Digest retry is still running (${queued.job_id.slice(0, 8)}).` };
      const parsed = parseDigestJobResult(job.result);
      if (parsed?.duplicate) return { message: "Digest delivery was already sent." };
      return { message: "Digest delivery sent." };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
  shortlistBulk: async ({ request }) => {
    const formData = await request.formData();
    const runId = z
      .string()
      .trim()
      .max(128)
      .or(z.literal(""))
      .catch("")
      .parse(formData.get("run_id"));
    const ids = formData
      .getAll("ids")
      .map((value) => idSchema.safeParse(value))
      .filter((result): result is { success: true; data: number } => result.success)
      .map((result) => result.data);
    const uniqueIds = [...new Set(ids)];
    if (!uniqueIds.length) return fail(400, { message: "Select at least one book first." });
    try {
      const result = await engine<{
        updated?: number;
        decisions?: Array<{ id: number; decision_id: number }>;
      }>(
        "/api/recommendations/bulk-feedback",
        {
          method: "POST",
          body: JSON.stringify({
            ids: uniqueIds,
            action: "save",
            ...(runId ? { run_id: runId } : {}),
          }),
        },
      );
      const count = result.updated ?? uniqueIds.length;
      const undoDecisions = (result.decisions ?? []).filter(
        (item) =>
          idSchema.safeParse(item?.id).success &&
          idSchema.safeParse(item?.decision_id).success,
      );
      return {
        message: `${count} book${count === 1 ? "" : "s"} added to your shortlist.`,
        ...(undoDecisions.length
          ? {
              undo_decisions: undoDecisions,
              undo_label:
                undoDecisions.length === 1
                  ? "shortlisting this book"
                  : `shortlisting ${undoDecisions.length} books`,
            }
          : {}),
      };
    } catch (error) {
      return fail(engineStatus(error), { message: message(error) });
    }
  },
};

async function waitForJob(jobId: string, maxAttempts = 30, delayMs = 500) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    const job = await engine<{
      status: string;
      error: string | null;
      result: string | null;
    }>(`/api/jobs/${jobId}`);
    if (job.status === "complete" || job.status === "failed") return job;
  }
  return { status: "queued", error: null, result: null };
}

type DigestJobResult = {
  status?: string;
  reason?: string;
  period?: string;
  items?: number;
  duplicate?: boolean;
  error?: string;
  deliveries?: Array<{ status?: string; error?: string }>;
};

function parseDigestJobResult(result: string | null): DigestJobResult | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    return parsed && typeof parsed === "object" ? (parsed as DigestJobResult) : null;
  } catch {
    return null;
  }
}

function digestRunMessage(result: string | null, jobId: string, jobStatus: string) {
  if (jobStatus !== "complete") {
    return `Digest is still running (${jobId.slice(0, 8)}).`;
  }
  const parsed = parseDigestJobResult(result);
  if (parsed?.status === "skipped") {
    if (parsed.reason === "no_new_recommendations") {
      return "Digest skipped: no new recommendations matched your minimum score.";
    }
    if (parsed.reason === "already_delivered") {
      return parsed.period
        ? `Digest skipped: ${parsed.period} was already delivered.`
        : "Digest skipped: this week's digest was already delivered.";
    }
    if (parsed.reason === "no_channels") {
      return "Digest skipped: enable and save at least one delivery channel first.";
    }
    return "Digest skipped.";
  }
  if (parsed?.status === "sent") {
    const count = typeof parsed.items === "number" ? parsed.items : 0;
    return `Digest sent with ${count} book${count === 1 ? "" : "s"}.`;
  }
  return "Digest run completed.";
}

function digestJobFailure(result: string | null) {
  const parsed = parseDigestJobResult(result);
  if (!parsed || parsed.status !== "failed") return null;
  if (typeof parsed.error === "string" && parsed.error.trim()) {
    return parsed.error.trim();
  }
  const deliveryError = parsed.deliveries?.find(
    (delivery) =>
      delivery.status === "failed" &&
      typeof delivery.error === "string" &&
      delivery.error.trim(),
  )?.error;
  if (deliveryError) return deliveryError.trim();
  if (parsed.reason === "retry_limit_reached") {
    return "Digest delivery reached the automatic retry limit.";
  }
  return "The digest channel could not deliver the message.";
}

function parseAuditRemaining(result: string | null): number | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result) as { remaining?: unknown };
    return typeof parsed.remaining === "number" &&
      Number.isFinite(parsed.remaining) &&
      parsed.remaining >= 0
      ? Math.floor(parsed.remaining)
      : null;
  } catch {
    return null;
  }
}

function normalizeCatalogQuality(raw: unknown): CatalogQuality {
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const auditRaw =
    record.audit && typeof record.audit === "object"
      ? (record.audit as Record<string, unknown>)
      : {};
  const validStatuses = new Set([
    "idle",
    "queued",
    "running",
    "failed",
    "paused",
    "complete",
  ]);
  const status = validStatuses.has(String(auditRaw.status ?? ""))
    ? (String(auditRaw.status) as CatalogAuditStatus["status"])
    : "idle";
  const mode = auditRaw.mode === "full" ? "full" : "incremental";
  const lastMode =
    auditRaw.last_mode === "full" || auditRaw.last_mode === "incremental"
      ? auditRaw.last_mode
      : null;
  const remaining =
    typeof auditRaw.remaining === "number" &&
    Number.isFinite(auditRaw.remaining) &&
    auditRaw.remaining >= 0
      ? Math.floor(auditRaw.remaining)
      : 0;
  const progress =
    typeof auditRaw.progress === "number" && Number.isFinite(auditRaw.progress)
      ? Math.max(0, Math.min(1, auditRaw.progress))
      : 0;
  const batchSize =
    typeof auditRaw.batch_size === "number" &&
    Number.isInteger(auditRaw.batch_size) &&
    auditRaw.batch_size > 0
      ? auditRaw.batch_size
      : 50;
  const subbatchSize =
    typeof auditRaw.subbatch_size === "number" &&
    Number.isInteger(auditRaw.subbatch_size) &&
    auditRaw.subbatch_size > 0
      ? auditRaw.subbatch_size
      : 10;
  const statuses = Array.isArray(record.statuses)
    ? record.statuses
        .filter(
          (item): item is { quality_status: string; count: number } =>
            Boolean(item && typeof item === "object") &&
            typeof (item as Record<string, unknown>).quality_status === "string" &&
            typeof (item as Record<string, unknown>).count === "number",
        )
        .map((item) => ({
          quality_status: item.quality_status,
          count: item.count,
        }))
    : [];
  const identifiersRaw =
    record.identifiers && typeof record.identifiers === "object"
      ? (record.identifiers as Record<string, unknown>)
      : {};
  const flagsRaw =
    record.flags && typeof record.flags === "object" && !Array.isArray(record.flags)
      ? (record.flags as Record<string, unknown>)
      : {};
  const flags: Record<string, number> = {};
  for (const [key, value] of Object.entries(flagsRaw)) {
    if (typeof value === "number" && Number.isFinite(value)) {
      flags[key] = value;
    }
  }
  return {
    statuses,
    identifiers: {
      total: typeof identifiersRaw.total === "number" ? identifiersRaw.total : 0,
      with_isbn13:
        typeof identifiersRaw.with_isbn13 === "number"
          ? identifiersRaw.with_isbn13
          : 0,
      with_catalog_match:
        typeof identifiersRaw.with_catalog_match === "number"
          ? identifiersRaw.with_catalog_match
          : 0,
    },
    flags,
    version: typeof record.version === "string" ? record.version : "",
    audit: {
      status,
      active: Boolean(auditRaw.active),
      mode,
      last_mode: lastMode,
      batch_size: batchSize,
      subbatch_size: subbatchSize,
      remaining,
      progress,
      started_at:
        typeof auditRaw.started_at === "string" && auditRaw.started_at
          ? auditRaw.started_at
          : null,
      completed_at:
        typeof auditRaw.completed_at === "string" && auditRaw.completed_at
          ? auditRaw.completed_at
          : null,
      job_id:
        typeof auditRaw.job_id === "string" && auditRaw.job_id
          ? auditRaw.job_id
          : null,
      last_error:
        typeof auditRaw.last_error === "string" && auditRaw.last_error
          ? auditRaw.last_error
          : null,
      can_retry: Boolean(auditRaw.can_retry || status === "failed"),
    },
  };
}
