import type { RequestHandler } from "./$types";
import { engine, engineStatus } from "#lib/server/engine.js";
import {
  INVALID_LLM_RERANK_MODE_MESSAGE,
  type LlmRerankMode,
  parseLlmRerankMode,
} from "#lib/llm-rerank.js";

type LlmModelEntry = { id: string; name: string };

type LlmCatalogResponse = {
  mode?: LlmRerankMode;
  api_url?: string;
  models?: LlmModelEntry[];
  providers?: Array<{
    id: string;
    name: string;
    api: string;
    npm: string;
    models: LlmModelEntry[];
  }>;
  cached?: boolean;
  stale?: boolean;
};

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Could not load available models.";

export const GET: RequestHandler = async ({ url }) => {
  try {
    const mode = parseLlmRerankMode(url.searchParams.get("mode"), "cloud");
    if (!mode) {
      return Response.json(
        { message: INVALID_LLM_RERANK_MODE_MESSAGE },
        { status: 400 },
      );
    }
    const apiUrl = (url.searchParams.get("api_url") || "").trim();
    const params = new URLSearchParams();
    if (mode !== "cloud") params.set("mode", mode);
    if (apiUrl) params.set("api_url", apiUrl);
    const query = params.toString();
    const catalog = await engine<LlmCatalogResponse>(
      query ? `/api/llm/models?${query}` : "/api/llm/models",
    );
    return Response.json(catalog);
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};

export const POST: RequestHandler = async ({ request }) => {
  try {
    const payload = (await request.json().catch(() => ({}))) as {
      mode?: unknown;
      api_url?: unknown;
      api_key?: unknown;
      use_saved_key?: unknown;
    };
    const mode = parseLlmRerankMode(payload.mode, "cloud");
    if (!mode) {
      return Response.json(
        { message: INVALID_LLM_RERANK_MODE_MESSAGE },
        { status: 400 },
      );
    }
    const catalog = await engine<LlmCatalogResponse>("/api/llm/models", {
      method: "POST",
      body: JSON.stringify({
        mode,
        api_url: typeof payload.api_url === "string" ? payload.api_url : "",
        api_key: typeof payload.api_key === "string" ? payload.api_key : "",
        use_saved_key: payload.use_saved_key === true,
      }),
    });
    return Response.json(catalog);
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
