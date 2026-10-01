import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { EngineError, engine } from "$lib/server/engine";

type ReadingHistoryScore = {
  id: number;
  algorithm_score: number | null;
  algorithm_explanation: string[];
  metadata_confidence: number | null;
};

const engineStatus = (error: unknown) =>
  error instanceof EngineError && error.status < 500 ? error.status : 502;
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Reading history scores could not be loaded.";

export const GET: RequestHandler = async ({ url }) => {
  const offset = Number(url.searchParams.get("offset") ?? 0);
  const limit = Number(url.searchParams.get("limit") ?? 20);
  if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(limit) || limit < 1 || limit > 20) {
    return json({ message: "Invalid reading history score range." }, { status: 400 });
  }

  try {
    const scores = await engine<ReadingHistoryScore[]>(
      `/api/reading-history/scores?offset=${offset}&limit=${limit}`,
    );
    return json({ scores });
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
