import type { RequestHandler } from "./$types";
import { engineStatus, engineWithHeaders } from "#lib/server/engine.js";
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
  seed_run_id: z
    .string()
    .trim()
    .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/)
    .optional(),
  search: z.string().trim().max(200).optional(),
  genre: z.string().trim().max(100).optional(),
});

const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Recommendations could not be loaded.";

export const GET: RequestHandler = async ({ url }) => {
  const parsed = querySchema.safeParse(Object.fromEntries(url.searchParams));
  if (!parsed.success) return Response.json({ message: "Choose a valid recommendation page." }, { status: 400 });

  const params = new URLSearchParams({
    status: parsed.data.status,
    limit: String(parsed.data.limit),
    offset: String(parsed.data.offset),
    ...(parsed.data.seed_run_id ? { seed_run_id: parsed.data.seed_run_id } : {}),
  });
  if (parsed.data.search) params.set("search", parsed.data.search);
  if (parsed.data.genre) params.set("genre", parsed.data.genre);
  try {
    const { data: items, headers } = await engineWithHeaders<Recommendation[]>(
      `/api/recommendations?${params.toString()}`,
    );
    const hasMoreHeader = headers.get("x-bookward-has-more")?.trim().toLowerCase();
    const has_more =
      hasMoreHeader === "true"
        ? true
        : hasMoreHeader === "false"
          ? false
          : items.length >= parsed.data.limit;
    const responseHeaders = new Headers();
    if (hasMoreHeader === "true" || hasMoreHeader === "false") {
      responseHeaders.set("X-Bookward-Has-More", hasMoreHeader);
    }
    const runId = headers.get("x-bookward-recommendation-run")?.trim();
    if (runId) {
      responseHeaders.set("X-Bookward-Recommendation-Run", runId);
    }
    return Response.json(
      {
        items: items.slice(0, parsed.data.limit).map(toPageBook),
        has_more,
      },
      { headers: responseHeaders },
    );
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
