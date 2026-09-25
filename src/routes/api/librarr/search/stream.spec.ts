import { afterEach, describe, expect, it, vi } from 'vitest';

const { engineStreamMock } = vi.hoisted(() => ({ engineStreamMock: vi.fn() }));

vi.mock('$lib/server/engine', () => ({
  engineStream: engineStreamMock,
  EngineError: class EngineError extends Error {
    constructor(message: string, readonly status: number) {
      super(message);
    }
  },
}));

import { GET } from './stream/+server';

afterEach(() => vi.clearAllMocks());

describe('Librarr streaming search endpoint', () => {
  it('passes SSE through as a live response and forwards cancellation', async () => {
    const chunks = [
      'event: started\ndata: {"search_id":"search-1"}\n\n',
      'event: complete\ndata: {"status":"complete"}\n\n',
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    });
    engineStreamMock.mockResolvedValue(
      new Response(stream, {
        headers: { 'content-type': 'text/event-stream; charset=utf-8' },
      }),
    );
    const request = new Request('http://bookward.test/api/librarr/search/stream?q=A+Book&media_type=ebook');

    const response = await GET({ url: new URL(request.url), request } as never);

    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe('text/event-stream; charset=utf-8');
    expect(response.headers.get('cache-control')).toBe('no-cache, no-transform');
    expect(response.headers.get('x-accel-buffering')).toBe('no');
    expect(engineStreamMock).toHaveBeenCalledWith(
      '/api/librarr/search/stream?q=A+Book&media_type=ebook',
      { signal: request.signal },
    );
    expect(await response.text()).toBe(chunks.join(''));
  });
});
