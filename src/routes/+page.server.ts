import { error as httpError, fail as kitFail } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { engine, EngineError } from "$lib/server/engine";
import { z } from "zod";
import { randomUUID } from "node:crypto";

type SourceFilters = {
  include_genres: string[];
  exclude_genres: string[];
};

type Overview = {
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
    lifecycle: "permanent" | "one_time";
    filters: SourceFilters;
    last_status: string | null;
    last_scanned_at: string | null;
  }>;
  settings: {
    embedding_backend: string;
    embedding_model: string;
    embedding_url: string;
    embedding_api_key_set: boolean;
    librarr_url: string;
    librarr_api_key_set: boolean;
    nyt_api_key_set?: boolean;
    librarr_media_type: "ebook" | "audiobook";
    source_sync_interval_hours: number;
    digest: DigestSettings;
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

const SESSION_COOKIE = "bookward_session";

export const load: PageServerLoad = async ({ url, cookies }) => {
  const existingSession = cookies?.get(SESSION_COOKIE);
  const sessionId = existingSession || randomUUID();
  if (cookies && !existingSession) {
    cookies.set(SESSION_COOKIE, sessionId, {
      path: "/",
      httpOnly: true,
      sameSite: "lax",
      secure: url.protocol === "https:",
      maxAge: 60 * 60 * 24 * 365,
    });
  }
  let overview: Overview;
  try {
    overview = await engine<Overview>("/api/overview?recommendation_limit=24", {
      headers: { "x-bookward-session": sessionId },
    });
  } catch (cause) {
    throw httpError(503, {
      message: `Bookward's recommendation engine is unavailable. ${message(cause)}`,
    });
  }
  const builtIn = overview.sources.find((source) => source.is_default);
  let digestPeriod = url.searchParams.get("digest_period");
  let digestReviewIds: number[] = [];
  if (digestPeriod) {
    try {
      const review = await engine<{ ids?: unknown[]; found?: boolean }>(
        `/api/digest/review?period=${encodeURIComponent(digestPeriod)}`,
      );
      digestReviewIds = Array.isArray(review.ids)
        ? review.ids.filter((value): value is number => Number.isInteger(value))
        : [];
      if (review.found === false) {
        digestPeriod = null;
      }
    } catch {
      // A stale digest link should still open the normal Discover view.
      digestReviewIds = [];
    }
  }
  return {
    recommendation_run_id: overview.recommendation_run_id ?? "",
    books: overview.recommendations.map((book) => ({
      ...book,
      cover_url: publicUrl(book.cover_url),
      source_url: publicUrl(book.source_url),
      published_on: book.release_date,
      published_kind: book.date_kind,
      synopsis: book.description,
      reason: book.explanation.join(" · "),
      source_type: "engine",
      librar_id: book.status === "imported" ? "imported" : null,
    })),
    history: overview.history,
    digestReview: { requested: Boolean(digestPeriod), ids: digestReviewIds },
    sources: overview.sources
      .filter((source) => !source.is_default)
      .map((source) => ({ ...source, label: source.name })),
    profile: {
      goodreads_url: "",
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
const urlSchema = z.string().url();
const fail = (statusCode: number, data: { message: string }) =>
  kitFail(statusCode, { ...data, error: true as const });
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The engine could not complete that request.";
const status = (error: unknown) =>
  error instanceof EngineError && error.status < 500 ? error.status : 502;
const publicUrl = (value: string) => {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
};

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
export const actions: Actions = {
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
      return fail(status(error), { message: message(error) });
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
      return fail(status(error), { message: message(error) });
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
      .enum(["saved", "rejected", "recommended"])
      .safeParse(data.get("status"));
    if (!id.success || !status.success)
      return fail(400, { message: "Invalid recommendation." });
    const action = {
      saved: "save",
      rejected: "reject",
      recommended: "restore",
    }[status.data];
    try {
      await engine(`/api/recommendations/${id.data}/feedback`, {
        method: "POST",
        body: JSON.stringify({ action, ...(runId ? { run_id: runId } : {}) }),
      });
      return {
        message:
          status.data === "saved"
            ? "Saved to your shortlist."
            : "Recommendation updated.",
      };
    } catch (error) {
      return fail(502, { message: message(error) });
    }
  },
  source: async ({ request }) => {
    const data = await request.formData();
    const url = urlSchema.safeParse(data.get("url"));
    const label = z
      .string()
      .trim()
      .min(2)
      .max(100)
      .safeParse(data.get("label"));
    const lifecycle = z
      .enum(["permanent", "one_time"])
      .catch("permanent")
      .parse(data.get("lifecycle"));
    const filters = sourceFiltersFromForm(data);
    if (!url.success || !label.success)
      return fail(400, { message: "Add a valid name and public URL." });
    try {
      const preview = await engine<{ count: number }>("/api/sources/preview", {
        method: "POST",
        body: JSON.stringify({ url: url.data, filters }),
      });
      if (preview.count === 0)
        return fail(400, {
          message: filters.include_genres.length || filters.exclude_genres.length
            ? "No books matched these filters."
            : "No recognizable books were found at that URL.",
        });
      await engine("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          name: label.data,
          url: url.data,
          lifecycle,
          filters,
        }),
      });
      return {
        message: `${lifecycle === "one_time" ? "One-time import" : "Permanent source"} added; its first scan is queued (${preview.count} book${preview.count === 1 ? "" : "s"} detected).`,
      };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  configureSourceFilters: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("id"));
    if (!id.success) return fail(400, { message: "Invalid source." });
    const filters = sourceFiltersFromForm(data);
    try {
      await engine(`/api/sources/${id.data}`, {
        method: "PUT",
        body: JSON.stringify({ filters }),
      });
      return { message: "Source filters updated; a fresh scan is queued." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
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
      return fail(status(error), { message: message(error) });
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
      return fail(status(error), { message: message(error) });
    }
  },
  toggleDefault: async ({ request }) => {
    try {
      const overview = await engine<Overview>("/api/overview");
      const source = overview.sources.find((item) => item.is_default);
      if (!source) return fail(404, { message: "Default source not found." });
      await engine(`/api/sources/${source.id}/toggle`, {
        method: "PUT",
        body: "{}",
      });
      return { message: "Upcoming-book source updated." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  connectGoodreads: async ({ request }) => {
    const url = urlSchema.safeParse((await request.formData()).get("url"));
    if (!url.success)
      return fail(400, { message: "Enter a valid Goodreads RSS URL." });
    try {
      const result = await engine<{ imported: number }>(
        "/api/import/goodreads/rss",
        { method: "POST", body: JSON.stringify({ url: url.data }) },
      );
      return { message: `Synced ${result.imported} books from Goodreads.` };
    } catch (error) {
      return fail(502, { message: message(error) });
    }
  },
  importGoodreadsCsv: async ({ request }) => {
    const incoming = await request.formData();
    const file = incoming.get("file");
    if (!(file instanceof File) || file.size === 0)
      return fail(400, { message: "Choose a Goodreads CSV export." });
    const body = new FormData();
    body.set("file", file, file.name);
    try {
      const result = await engine<{ imported: number }>(
        "/api/import/goodreads/csv",
        { method: "POST", body },
      );
      return { message: `Imported ${result.imported} Goodreads books.` };
    } catch (error) {
      return fail(502, { message: message(error) });
    }
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
      const overview = await engine<Overview>("/api/overview");
      if (!key.data && !overview.settings.librarr_api_key_set)
        return fail(400, {
          message: "Enter a Librarr API key for the first connection.",
        });
      await engine("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          embedding_backend: overview.settings.embedding_backend,
          embedding_model: overview.settings.embedding_model,
          embedding_url: overview.settings.embedding_url,
          embedding_api_key: "",
          librarr_url: url.data,
          librarr_api_key: key.data,
          librarr_media_type: mediaType.data,
        }),
      });
      return {
        message: `Librarr connection saved for ${mediaType.data === "ebook" ? "ebooks" : "audiobooks"}.`,
      };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  importLibrar: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid book." });
    try {
      await engine(`/api/recommendations/${id.data}/import`, {
        method: "POST",
        body: "{}",
      });
      return { message: "Book added to the Librarr wishlist." };
    } catch (error) {
      return fail(502, { message: message(error) });
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
      const overview = await engine<Overview>("/api/overview");
      await engine("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          embedding_backend: backend.data,
          embedding_model: model.data,
          embedding_url: String(data.get("url") || ""),
          embedding_api_key: String(data.get("apiKey") || ""),
          librarr_url: overview.settings.librarr_url,
          librarr_api_key: "",
          librarr_media_type: overview.settings.librarr_media_type,
        }),
      });
      return { message: "Embedding provider saved and checked." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  rebuildEmbeddings: async () => {
    try {
      const result = await engine<{ job_id: string }>(
        "/api/embeddings/rebuild",
        { method: "POST", body: "{}" },
      );
      for (let attempt = 0; attempt < 20; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        const job = await engine<{
          status: string;
          error: string | null;
          result: string | null;
        }>(`/api/jobs/${result.job_id}`);
        if (job.status === "complete")
          return {
            message: "Embeddings rebuilt and recommendations refreshed.",
          };
        if (job.status === "failed")
          return fail(502, {
            message: job.error || "Embedding rebuild failed.",
          });
      }
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
      for (let attempt = 0; attempt < 20; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        const job = await engine<{
          status: string;
          error: string | null;
          result: string | null;
        }>(`/api/jobs/${result.job_id}`);
        if (job.status === "complete") {
          try {
            const result = job.result ? JSON.parse(job.result) as { errors?: unknown[] } : {};
            if (Array.isArray(result.errors) && result.errors.length)
              return { message: `Sources refreshed with ${result.errors.length} source${result.errors.length === 1 ? "" : "s"} needing attention.` };
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
      }
      return {
        message: `Refresh is still running (${result.job_id.slice(0, 8)}).`,
      };
    } catch (error) {
      return fail(502, { message: message(error) });
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
      return fail(status(error), { message: message(error) });
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
      return fail(status(error), { message: message(error) });
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
      return { message: "Digest run queued; configured channels will receive it shortly." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  retryDigest: async ({ request }) => {
    const id = z.string().uuid().safeParse((await request.formData()).get("id"));
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
      return { message: "Digest delivery retry queued." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
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
      const result = await engine<{ updated?: number }>(
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
      return { message: `${result.updated ?? uniqueIds.length} book${(result.updated ?? uniqueIds.length) === 1 ? "" : "s"} added to your shortlist.` };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
};

async function waitForJob(jobId: string) {
  for (let attempt = 0; attempt < 30; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    const job = await engine<{
      status: string;
      error: string | null;
      result: string | null;
    }>(`/api/jobs/${jobId}`);
    if (job.status === "complete" || job.status === "failed") return job;
  }
  return { status: "queued", error: null, result: null };
}

function digestJobFailure(result: string | null) {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result) as {
      status?: string;
      deliveries?: Array<{ status?: string; error?: string }>;
    };
    if (parsed.status !== "failed") return null;
    return (
      parsed.deliveries?.find((delivery) => delivery.status === "failed")?.error ||
      "The digest channel could not deliver the message."
    );
  } catch {
    return null;
  }
}
