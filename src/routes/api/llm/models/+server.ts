import type { RequestHandler } from "./$types";
import { engine, engineStatus } from "#lib/server/engine.js";

type LlmCatalogResponse = {
  providers: Array<{
    id: string;
    name: string;
    api: string;
    npm: string;
    models: Array<{ id: string; name: string }>;
  }>;
  cached?: boolean;
  stale?: boolean;
};

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Could not load models.dev catalog.";

export const GET: RequestHandler = async () => {
  try {
    const catalog = await engine<LlmCatalogResponse>("/api/llm/models");
    return Response.json(catalog);
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
