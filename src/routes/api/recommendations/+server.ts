import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { EngineError, engine } from "$lib/server/engine";
import { toPageBook, type Recommendation } from "$lib/server/recommendations";
import { z } from "zod";

const querySchema = z.object({
  status: z.enum(["recommended", "saved", "imported", "all"]).default("recommended"),
  limit: z.coerce.number().int().min(1).max(24).default(8),
  offset: z.coerce.number().int().min(0).max(1_000_000).default(0),
});

const engineStatus = (error: unknown) => error instanceof EngineError && error.status < 500 ? error.status : 502;
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Recommendations could not be loaded.";

export const GET: RequestHandler = async ({ url }) => {
  const parsed = querySchema.safeParse(Object.fromEntries(url.searchParams));
  if (!parsed.success) return json({ message: "Choose a valid recommendation page." }, { status: 400 });

  const params = new URLSearchParams({
    status: parsed.data.status,
    limit: String(parsed.data.limit),
    offset: String(parsed.data.offset),
  });
  try {
    const items = await engine<Recommendation[]>(`/api/recommendations?${params.toString()}`);
    return json({
      items: items.map(toPageBook),
      has_more: items.length === parsed.data.limit,
    });
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
