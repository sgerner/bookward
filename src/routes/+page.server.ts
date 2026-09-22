import { error as httpError, fail as kitFail } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { engine, EngineError } from "$lib/server/engine";
import { z } from "zod";
import { randomUUID } from "node:crypto";

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
    llm_connections?: LlmConnectionSummary[];
    llm_policies?: LlmPolicySummary[];
  };
};

type LlmConnectionSummary = {
  id: number;
  name: string;
  provider_id: string;
  model_id: string;
  endpoint: string;
  auth_type: string;
  enabled: number;
  last_status: string | null;
  last_error: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
};

type LlmPolicySummary = {
  id: number;
  name: string;
  connection_id: number;
  enabled: number;
  top_k: number;
  prompt_version: string;
  created_at: string;
  updated_at: string;
  connection_name: string;
  provider_id: string;
  model_id: string;
  auth_type: string;
  reasoning_effort: string;
};

type LlmRunSummary = {
  id: string;
  policy_id: number;
  connection_id: number;
  status: string;
  candidate_count: number;
  latency_ms: number | null;
  created_at: string;
  finished_at: string | null;
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
  let llmRuns: LlmRunSummary[] = [];
  try {
    const runResult = await engine<{ runs?: Array<Record<string, unknown>> }>(
      "/api/llm/runs?limit=12",
    );
    llmRuns = normalizeLlmRuns(runResult.runs ?? []);
  } catch {
    // Optional history should not make the main recommendation page unavailable.
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
    llm: {
      connections: overview.settings.llm_connections ?? [],
      policies: overview.settings.llm_policies ?? [],
      runs: llmRuns,
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

const normalizeLlmRuns = (runs: Array<Record<string, unknown>>): LlmRunSummary[] =>
  runs.flatMap((run) => {
    if (
      typeof run.id !== "string" ||
      typeof run.policy_id !== "number" ||
      typeof run.connection_id !== "number" ||
      typeof run.status !== "string" ||
      typeof run.candidate_count !== "number" ||
      typeof run.created_at !== "string"
    )
      return [];
    return [
      {
        id: run.id,
        policy_id: run.policy_id,
        connection_id: run.connection_id,
        status: run.status,
        candidate_count: run.candidate_count,
        latency_ms: typeof run.latency_ms === "number" ? run.latency_ms : null,
        created_at: run.created_at,
        finished_at: typeof run.finished_at === "string" ? run.finished_at : null,
      },
    ];
  });

const formText = (data: FormData, key: string) => String(data.get(key) ?? "").trim();
const optionalFormText = (data: FormData, key: string) => {
  const value = formText(data, key);
  return value || undefined;
};
const llmAuthSchema = z.enum(["api_key", "openai_codex", "claude_code"]);
const llmProviderSchema = z
  .string()
  .trim()
  .min(1)
  .max(100)
  .regex(/^[A-Za-z0-9._-]+$/);
const llmModelSchema = z.string().trim().min(1).max(300);
const llmNameSchema = z.string().trim().min(1).max(100);
const llmEndpointSchema = z.string().trim().max(500);

export const actions: Actions = {
  loadLlmCatalog: async ({ request }) => {
    const refresh = (await request.formData()).get("refresh") === "on";
    try {
      const llmCatalog = await engine<Record<string, unknown>>(
        `/api/llm/catalog${refresh ? "?refresh=true" : ""}`,
      );
      return { llmCatalog, message: "Model catalog loaded." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  saveLlmConnection: async ({ request }) => {
    const data = await request.formData();
    const id = optionalFormText(data, "id");
    const name = llmNameSchema.safeParse(data.get("name"));
    const providerId = llmProviderSchema.safeParse(data.get("providerId"));
    const modelId = llmModelSchema.safeParse(data.get("modelId"));
    const endpoint = llmEndpointSchema.safeParse(data.get("endpoint") ?? "");
    const authType = llmAuthSchema.safeParse(data.get("authType"));
    const reasoningEffort = z.enum(["low", "medium", "high", "xhigh", "max"]).optional().safeParse(data.get("reasoningEffort") || undefined);
    if (!name.success || !providerId.success || !modelId.success || !endpoint.success || !authType.success || !reasoningEffort.success)
      return fail(400, { message: "Choose a provider, model, authentication method, and name." });
    const payload: Record<string, unknown> = {
      name: name.data,
      provider_id: providerId.data,
      model_id: modelId.data,
      endpoint: endpoint.data,
      auth_type: authType.data,
      enabled: data.get("enabled") !== "off",
    };
    const apiKey = optionalFormText(data, "apiKey");
    const oauthToken = optionalFormText(data, "oauthToken");
    if (apiKey) payload.api_key = apiKey;
    if (oauthToken) payload.oauth_token = oauthToken;
    if (authType.data === "claude_code" && reasoningEffort.data) payload.reasoning_effort = reasoningEffort.data;
    try {
      await engine(
        id ? `/api/llm/connections/${encodeURIComponent(id)}` : "/api/llm/connections",
        { method: id ? "PUT" : "POST", body: JSON.stringify(payload) },
      );
      return { message: `${name.data} connection saved.` };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  disableLlmConnection: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid LLM connection." });
    try {
      await engine(`/api/llm/connections/${id.data}`, { method: "DELETE", body: "" });
      return { message: "LLM connection disabled." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  openaiDeviceLoginStart: async () => {
    try {
      const deviceLogin = await engine<Record<string, unknown>>("/api/llm/openai/device/start", {
        method: "POST",
        body: "{}",
      });
      return { deviceLogin, message: "OpenAI device login started." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  openaiDeviceLoginStatus: async () => {
    try {
      const deviceLogin = await engine<Record<string, unknown>>("/api/llm/openai/device/status");
      return { deviceLogin };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  openaiDeviceLoginCancel: async ({ request }) => {
    const loginId = z.string().trim().min(1).max(200).safeParse(
      (await request.formData()).get("loginId"),
    );
    if (!loginId.success) return fail(400, { message: "The device login has expired." });
    try {
      const deviceLogin = await engine<Record<string, unknown>>("/api/llm/openai/device/cancel", {
        method: "POST",
        body: JSON.stringify({ login_id: loginId.data }),
      });
      return { deviceLogin, message: "OpenAI device login cancelled." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  openaiDeviceLogout: async () => {
    try {
      const deviceLogin = await engine<Record<string, unknown>>("/api/llm/openai/device/logout", {
        method: "POST",
        body: "{}",
      });
      return { deviceLogin, message: "OpenAI subscription signed out." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  saveClaudeOAuth: async ({ request }) => {
    const data = await request.formData();
    const connectionId = idSchema.safeParse(data.get("connectionId"));
    const token = z.string().trim().min(1).max(20_000).safeParse(data.get("oauthToken"));
    if (!connectionId.success || !token.success)
      return fail(400, { message: "Choose an Anthropic connection and enter its OAuth token." });
    try {
      await engine("/api/llm/claude/oauth", {
        method: "POST",
        body: JSON.stringify({ connection_id: connectionId.data, oauth_token: token.data }),
      });
      return { message: "Claude Code OAuth token saved." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  saveLlmPolicy: async ({ request }) => {
    const data = await request.formData();
    const id = optionalFormText(data, "id");
    const name = llmNameSchema.safeParse(data.get("name"));
    const connectionId = idSchema.safeParse(data.get("connectionId"));
    const topK = z.coerce.number().int().min(1).max(100).safeParse(data.get("topK"));
    const promptVersion = z.string().trim().min(1).max(100).safeParse(data.get("promptVersion") || "shadow-v1");
    if (!name.success || !connectionId.success || !topK.success || !promptVersion.success)
      return fail(400, { message: "Choose a policy name, connection, and candidate count." });
    const payload = {
      name: name.data,
      connection_id: connectionId.data,
      top_k: topK.data,
      prompt_version: promptVersion.data,
      enabled: data.get("enabled") !== "off",
    };
    try {
      await engine(
        id ? `/api/llm/policies/${encodeURIComponent(id)}` : "/api/llm/policies",
        { method: id ? "PUT" : "POST", body: JSON.stringify(payload) },
      );
      return { message: `${name.data} policy saved.` };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  saveLlmPolicySettings: async ({ request }) => {
    const data = await request.formData();
    const id = idSchema.safeParse(data.get("policyId"));
    const modelId = llmModelSchema.safeParse(data.get("modelId"));
    const reasoningEffort = z.enum(["low", "medium", "high", "xhigh", "max"]).safeParse(data.get("reasoningEffort"));
    if (!id.success || !modelId.success || !reasoningEffort.success)
      return fail(400, { message: "Choose a subscription model and reasoning level." });
    try {
      await engine(`/api/llm/policies/${id.data}/settings`, {
        method: "PUT",
        body: JSON.stringify({ model_id: modelId.data, reasoning_effort: reasoningEffort.data }),
      });
      return { message: "Subscription shadow settings saved." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  disableLlmPolicy: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid shadow policy." });
    try {
      await engine(`/api/llm/policies/${id.data}`, { method: "DELETE", body: "" });
      return { message: "Shadow policy disabled." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  runLlmPolicy: async ({ request }) => {
    const id = idSchema.safeParse((await request.formData()).get("id"));
    if (!id.success) return fail(400, { message: "Invalid shadow policy." });
    try {
      const result = await engine<{ job_id?: string }>(`/api/llm/policies/${id.data}/run`, {
        method: "POST",
        body: "{}",
      });
      return { message: `Shadow run queued${result.job_id ? ` (${result.job_id.slice(0, 8)})` : ""}.` };
    } catch (error) {
      return fail(status(error), { message: message(error) });
    }
  },
  refreshLlmRuns: async () => {
    try {
      const result = await engine<{ runs?: Array<Record<string, unknown>> }>("/api/llm/runs?limit=12");
      return { llmRuns: normalizeLlmRuns(result.runs ?? []), message: "Run history refreshed." };
    } catch (error) {
      return fail(status(error), { message: message(error) });
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
    if (!url.success || !label.success)
      return fail(400, { message: "Add a valid name and public URL." });
    try {
      const preview = await engine<{ count: number }>("/api/sources/preview", {
        method: "POST",
        body: JSON.stringify({ url: url.data }),
      });
      if (preview.count === 0)
        return fail(400, {
          message: "No recognizable books were found at that URL.",
        });
      await engine("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          name: label.data,
          url: url.data,
          lifecycle,
        }),
      });
      return {
        message: `${lifecycle === "one_time" ? "One-time import" : "Permanent source"} added; its first scan is queued (${preview.count} book${preview.count === 1 ? "" : "s"} detected).`,
      };
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
