import { describe, expect, it } from 'vitest';
import { readLibrarrSearchStream } from './librarr-stream';

describe('readLibrarrSearchStream', () => {
  it('handles split CRLF frames and reports cumulative result snapshots as they arrive', async () => {
    const batches: string[][] = [];
    const chunks = [
      'event: started\r\ndata: {"search":"x"}\r\n\r',
      '\nevent: results\r\ndata: {"results":[{"title":"First"}]}\r\n\r\n',
      'event: results\ndata: {"results":[{"title":"First"},{"title":"Second"}]}\n\n',
      'event: complete\ndata: {"status":"complete"}\n\n',
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    });

    const result = await readLibrarrSearchStream(new Response(stream), (books) => {
      batches.push(books.map((book) => String(book.title)));
    });

    expect(batches).toEqual([['First'], ['First', 'Second']]);
    expect(result.completed).toBe(true);
  });

  it('preserves partial results when the stream ends without a complete event', async () => {
    const batches: string[][] = [];
    const response = new Response(
      'event: results\ndata: {"results":[{"title":"Partial"}]}\n\n',
    );

    const result = await readLibrarrSearchStream(response, (books) => {
      batches.push(books.map((book) => String(book.title)));
    });

    expect(batches).toEqual([['Partial']]);
    expect(result.completed).toBe(false);
  });

  it('accepts an empty complete event frame', async () => {
    const response = new Response('event: complete\n\n');

    await expect(readLibrarrSearchStream(response, () => {})).resolves.toEqual({
      completed: true,
    });
  });
});
