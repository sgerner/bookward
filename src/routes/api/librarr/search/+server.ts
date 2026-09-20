import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engine } from '$lib/server/engine';

const engineStatus = (error: unknown) => error instanceof EngineError && error.status < 500 ? error.status : 502;
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Librarr search failed.';

export const GET: RequestHandler = async ({ url }) => {
  const query = url.searchParams.get('q')?.trim() ?? '';
  const mediaType = url.searchParams.get('media_type') ?? 'audiobook';
  if (query.length < 2 || query.length > 200 || !['ebook', 'audiobook'].includes(mediaType)) {
    return json({ message: 'Enter at least two characters and choose a valid media type.' }, { status: 400 });
  }
  const params = new URLSearchParams({ q: query, media_type: mediaType });
  try {
    return json(await engine(`/api/librarr/search?${params.toString()}`));
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
