import type { RequestHandler } from "./$types";
import { engine, engineStatus } from "#lib/server/engine.js";

type LlmModelEntry = { id: string; name: string };

type LlmCatalogResponse = {
  mode?: "cloud" | "ollama" | "openai-compatible";
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
    const mode = (url.searchParams.get("mode") || "").trim();
    const apiUrl = (url.searchParams.get("api_url") || "").trim();
    const useSavedKey = url.searchParams.get("use_saved_key") === "true";
    const params = new URLSearchParams();
    if (mode && mode !== "cloud") params.set("mode", mode);
    if (apiUrl) params.set("api_url", apiUrl);
    if (useSavedKey) params.set("use_saved_key", "true");
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
    const catalog = await engine<LlmCatalogResponse>("/api/llm/models", {
      method: "POST",
      body: JSON.stringify({
        mode: typeof payload.mode === "string" ? payload.mode : "cloud",
        api_url: typeof payload.api_url === "string" ? payload.api_url : "",
        api_key: typeof payload.api_key === "string" ? payload.api_key : "",
        use_saved_key: payload.use_saved_key !== false,
      }),
    });
    return Response.json(catalog);
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
