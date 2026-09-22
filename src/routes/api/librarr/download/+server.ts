import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engine } from '$lib/server/engine';

const engineStatus = (error: unknown) => error instanceof EngineError && error.status < 500 ? error.status : 502;
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Librarr download failed.';

export const POST: RequestHandler = async ({ request }) => {
  const payload = await request.json().catch(() => null) as { media_type?: unknown; candidate_id?: unknown; result?: unknown } | null;
  const mediaType = payload?.media_type;
  if (!payload || !['ebook', 'audiobook'].includes(String(mediaType)) || !payload.result || typeof payload.result !== 'object' || Array.isArray(payload.result)) {
    return json({ message: 'Choose a valid Librarr result and media type.' }, { status: 400 });
  }
  const candidateId = payload.candidate_id == null ? undefined : Number(payload.candidate_id);
  if (candidateId !== undefined && (!Number.isInteger(candidateId) || candidateId <= 0)) {
    return json({ message: 'Choose a valid recommendation.' }, { status: 400 });
  }
  try {
    return json(await engine('/api/librarr/download', { method: 'POST', body: JSON.stringify({ media_type: mediaType, candidate_id: candidateId, result: payload.result }) }));
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
