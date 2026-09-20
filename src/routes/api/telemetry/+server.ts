import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { engine } from "$lib/server/engine";

const MAX_EVENTS = 100;

export const POST: RequestHandler = async ({ request }) => {
  const payload = await request.json().catch(() => null) as {
    events?: unknown;
  } | null;
  if (!payload || !Array.isArray(payload.events) || payload.events.length < 1 || payload.events.length > MAX_EVENTS) {
    return new Response(null, { status: 204 });
  }
  try {
    await engine("/api/telemetry/events", {
      method: "POST",
      body: JSON.stringify({ events: payload.events }),
    });
  } catch {
    // Telemetry is deliberately best effort. A reader should never see a
    // recommendation action fail because the optional event path is down.
  }
  return json({ accepted: true }, { status: 202 });
};
