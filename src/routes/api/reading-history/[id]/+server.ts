import type { RequestHandler } from "./$types";
import {
  RATING_VALIDATION_MESSAGE,
  parseReadingHistoryId,
  parseWholeStarRating,
} from "#lib/reading-history-ratings.js";
import { engine, engineStatus } from "#lib/server/engine.js";

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

const READ_DATE_VALIDATION_MESSAGE =
  "Enter a valid completion date (YYYY-MM-DD), or null to clear.";

function parseReadAtUpdate(raw: unknown): { ok: true; value: string | null } | { ok: false; message: string } {
  if (raw === null) {
    return { ok: true, value: null };
  }
  if (typeof raw !== "string") {
    return { ok: false, message: READ_DATE_VALIDATION_MESSAGE };
  }
  const cleaned = raw.trim();
  if (!cleaned) {
    return { ok: false, message: READ_DATE_VALIDATION_MESSAGE };
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(cleaned)) {
    return { ok: false, message: READ_DATE_VALIDATION_MESSAGE };
  }
  const [year, month, day] = cleaned.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    Number.isNaN(parsed.valueOf()) ||
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() + 1 !== month ||
    parsed.getUTCDate() !== day
  ) {
    return { ok: false, message: READ_DATE_VALIDATION_MESSAGE };
  }
  const todayUtc = new Date().toISOString().slice(0, 10);
  if (cleaned > todayUtc) {
    return { ok: false, message: "Completion date cannot be in the future." };
  }
  return { ok: true, value: cleaned };
}

const updateErrorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Reading history could not be saved.";

export const PUT: RequestHandler = async ({ params, request }) => {
  const parsedId = parseReadingHistoryId(params.id);
  if (!parsedId.ok) {
    return Response.json({ message: parsedId.message }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ message: "Send a valid JSON request body." }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return Response.json({ message: "Send a valid JSON request body." }, { status: 400 });
  }

  const record = body as Record<string, unknown>;
  const hasRating = Object.prototype.hasOwnProperty.call(record, "rating");
  const hasReadAt = Object.prototype.hasOwnProperty.call(record, "read_at");
  if (!hasRating && !hasReadAt) {
    return Response.json({ message: RATING_VALIDATION_MESSAGE }, { status: 400 });
  }

  const payload: { rating?: number | null; read_at?: string | null } = {};
  if (hasRating) {
    const parsedRating = parseWholeStarRating(record.rating);
    if (!parsedRating.ok) {
      return Response.json({ message: parsedRating.message }, { status: 400 });
    }
    payload.rating = parsedRating.value;
  }
  if (hasReadAt) {
    const parsedReadAt = parseReadAtUpdate(record.read_at);
    if (!parsedReadAt.ok) {
      return Response.json({ message: parsedReadAt.message }, { status: 400 });
    }
    payload.read_at = parsedReadAt.value;
  }

  try {
    const updated = await engine<ReadingHistoryRatingResponse>(
      `/api/reading-history/${parsedId.value}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    );
    const { job_id = null, ...item } = updated;
    return Response.json({ item, job_id });
  } catch (error) {
    return Response.json({ message: updateErrorMessage(error) }, { status: engineStatus(error) });
  }
};
