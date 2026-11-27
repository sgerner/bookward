import type { RequestHandler } from "./$types";
import { engine, engineStatus } from "#lib/server/engine.js";
import {
  INVALID_LLM_RERANK_MODE_MESSAGE,
  type LlmRerankMode,
  parseLlmRerankMode,
} from "#lib/llm-rerank.js";

type LlmTestResponse = {
  ok: boolean;
  mode: LlmRerankMode;
  model: string;
  latency_ms?: number;
};

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "LLM reranker connection check failed.";

export const POST: RequestHandler = async ({ request }) => {
  try {
    const payload = (await request.json().catch(() => ({}))) as {
      mode?: unknown;
      api_url?: unknown;
      model?: unknown;
      api_key?: unknown;
      use_saved_key?: unknown;
    };
    const mode = parseLlmRerankMode(payload.mode, "ollama");
    if (!mode) {
      return Response.json(
        { message: INVALID_LLM_RERANK_MODE_MESSAGE },
        { status: 400 },
      );
    }
    const result = await engine<LlmTestResponse>("/api/llm/test", {
      method: "POST",
      body: JSON.stringify({
        mode,
        api_url: typeof payload.api_url === "string" ? payload.api_url : "",
        model: typeof payload.model === "string" ? payload.model : "",
        api_key: typeof payload.api_key === "string" ? payload.api_key : "",
        use_saved_key: payload.use_saved_key === true,
      }),
    });
    return Response.json(result);
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
