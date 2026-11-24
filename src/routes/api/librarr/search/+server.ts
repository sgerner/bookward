import type { RequestHandler } from './$types';
import { engineStatus, engine } from '#lib/server/engine.js';

const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Librarr search failed.';

export const GET: RequestHandler = async ({ url }) => {
  const query = url.searchParams.get('q')?.trim() ?? '';
  const mediaType = url.searchParams.get('media_type') ?? 'audiobook';
  if (query.length < 2 || query.length > 200 || !['ebook', 'audiobook'].includes(mediaType)) {
    return Response.json({ message: 'Enter at least two characters and choose a valid media type.' }, { status: 400 });
  }
  const params = new URLSearchParams({ q: query, media_type: mediaType });
  try {
    return Response.json(await engine(`/api/librarr/search?${params.toString()}`));
  } catch (error) {
    return Response.json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
