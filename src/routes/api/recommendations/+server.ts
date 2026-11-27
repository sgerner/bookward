import type { RequestHandler } from "./$types";
import { engineStatus, engine } from "#lib/server/engine.js";
import { toPageBook, type Recommendation } from "#lib/server/recommendations.js";
import { z } from "zod";

const querySchema = z.object({
  status: z.enum([
    "recommended",
    "saved",
    "imported",
    "all",
    "decisions",
    "rejected",
    "maybe_later",
  ]).default("recommended"),
  limit: z.coerce.number().int().min(1).max(24).default(8),
  offset: z.coerce.number().int().min(0).max(1_000_000).default(0),
  run_id: z.string().trim().min(8).max(128).optional(),
  start_run: z.enum(["1", "true"]).optional(),
  track_run: z.enum(["0", "false", "1", "true"]).optional(),
  search: z.string().trim().max(200).optional(),
  genre: z.string().trim().max(100).optional(),
});

const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Recommendations could not be loaded.";

export const GET: RequestHandler = async ({ url }) => {
  const parsed = querySchema.safeParse(Object.fromEntries(url.searchParams));
  if (!parsed.success) return Response.json({ message: "Choose a valid recommendation page." }, { status: 400 });

  const params = new URLSearchParams({
    status: parsed.data.status,
    limit: String(parsed.data.limit + 1),
    offset: String(parsed.data.offset),
  });
  if (parsed.data.run_id) {
    params.set("run_id", parsed.data.run_id);
    params.set("impression_limit", String(parsed.data.limit));
  } else if (parsed.data.start_run) {
    params.set("impression_limit", String(parsed.data.limit));
  }
  if (parsed.data.track_run === "0" || parsed.data.track_run === "false") {
    params.set("track_run", "false");
  }
  if (parsed.data.search) params.set("search", parsed.data.search);
  if (parsed.data.genre) params.set("genre", parsed.data.genre);
  try {
    const items = await engine<Recommendation[]>(`/api/recommendations?${params.toString()}`);
    const mapped = items.slice(0, parsed.data.limit).map(toPageBook);
    const resolvedRunId =
      parsed.data.run_id ??
      mapped.find((item) => item.recommendation_run_id)?.recommendation_run_id;
    return Response.json({
      items: mapped,
      has_more: items.length > parsed.data.limit,
      ...(resolvedRunId ? { recommendation_run_id: resolvedRunId } : {}),
    });
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
