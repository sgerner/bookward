import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import {
  parseReadingHistoryId,
  parseWholeStarRating,
} from "#lib/reading-history-ratings.js";
import { engine, engineStatus } from "#lib/server/engine.js";

export type ReadingHistoryItem = {
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

type ReadingHistoryRatingResponse = {
  id: number;
  title: string;
  author: string;
  rating: number | null;
  read_at: string | null;
  source: string;
  created_at: string;
  job_id: string | null;
};

const loadErrorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Reading history could not be loaded.";

const updateErrorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Rating could not be saved.";

export const GET: RequestHandler = async () => {
  try {
    const items = await engine<ReadingHistoryItem[]>("/api/reading-history");
    return json({ items });
  } catch (error) {
    return json({ message: loadErrorMessage(error) }, { status: engineStatus(error) });
  }
};

const updateRating: RequestHandler = async ({ request }) => {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return json({ message: "Send a valid JSON request body." }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return json({ message: "Send a valid JSON request body." }, { status: 400 });
  }

  const record = body as Record<string, unknown>;
  const parsedId = parseReadingHistoryId(record.id);
  if (!parsedId.ok) {
    return json({ message: parsedId.message }, { status: 400 });
  }
  const parsedRating = parseWholeStarRating(record.rating);
  if (!parsedRating.ok) {
    return json({ message: parsedRating.message }, { status: 400 });
  }

  try {
    const updated = await engine<ReadingHistoryRatingResponse>(
      `/api/reading-history/${parsedId.value}`,
      {
        method: "PUT",
        body: JSON.stringify({ rating: parsedRating.value }),
      },
    );
    const { job_id = null, ...item } = updated;
    return json({ item, job_id });
  } catch (error) {
    return json({ message: updateErrorMessage(error) }, { status: engineStatus(error) });
  }
};

export const PUT = updateRating;
export const PATCH = updateRating;
