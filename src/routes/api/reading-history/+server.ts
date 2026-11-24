import type { RequestHandler } from "./$types";
import { engineStatus, engine } from "#lib/server/engine.js";

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

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Reading history could not be loaded.";

export const GET: RequestHandler = async () => {
  try {
    const items = await engine<ReadingHistoryItem[]>("/api/reading-history");
    return Response.json({ items });
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
