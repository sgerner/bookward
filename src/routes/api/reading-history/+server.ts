import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { EngineError, engine } from "$lib/server/engine";

type ReadingHistoryItem = {
  id: number;
  title: string;
  author: string;
  rating: number | null;
  read_at: string | null;
  source: string;
  created_at: string;
  rank_score: number | null;
  algorithm_score: number | null;
  algorithm_explanation: string[];
  metadata_confidence: number | null;
};

const engineStatus = (error: unknown) =>
  error instanceof EngineError && error.status < 500 ? error.status : 502;
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Reading history could not be loaded.";

export const GET: RequestHandler = async () => {
  try {
    const items = await engine<ReadingHistoryItem[]>("/api/reading-history");
    return json({ items });
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
