import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { EngineError, engineStream } from '$lib/server/engine';

const engineStatus = (error: unknown) => {
  if (!(error instanceof EngineError)) return 502;
  return error.status < 500 || error.status === 501 ? error.status : 502;
};
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Librarr search failed.';

export const GET: RequestHandler = async ({ url, request }) => {
  const query = url.searchParams.get('q')?.trim() ?? '';
  const mediaType = url.searchParams.get('media_type') ?? 'audiobook';
  if (query.length < 2 || query.length > 200 || !['ebook', 'audiobook'].includes(mediaType)) {
    return json({ message: 'Enter at least two characters and choose a valid media type.' }, { status: 400 });
  }
  const params = new URLSearchParams({ q: query, media_type: mediaType });
  try {
    const upstream = await engineStream(`/api/librarr/search/stream?${params.toString()}`, {
      signal: request.signal
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-transform',
        'x-accel-buffering': 'no'
      }
    });
  } catch (error) {
    return json({ message: errorMessage(error) }, { status: engineStatus(error) });
  }
};
