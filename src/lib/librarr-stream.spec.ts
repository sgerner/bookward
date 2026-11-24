import { describe, expect, it } from 'vitest';
import {
  buildLibrarrDownloadPayload,
  canRetryLibrarrSearch,
  finalizeLibrarrStreamState,
  librarrFormatChangeAction,
  parseFrame,
  parseStreamErrorMessage,
  readLibrarrSearchStream,
  resolveLibrarrResultFormat,
  shouldShowLibrarrEmptyBanner,
} from './librarr-stream';

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

  it.each([
    ['{"message":"Indexer timed out."}', 'Indexer timed out.'],
    ['{"error":"Librarr indexer unavailable"}', 'Librarr indexer unavailable'],
    ['{"detail":"Upstream rate limit exceeded"}', 'Upstream rate limit exceeded'],
    ['{}', 'Librarr search failed.'],
    ['', 'Librarr search failed.'],
  ])('throws on SSE error frames and cancels the reader (%s)', async (payload, expectedMessage) => {
    let cancelReason: unknown = null;
    const batches: string[][] = [];
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode('event: results\ndata: {"results":[{"title":"Before error"}]}\n\n'),
        );
        const dataLine = payload ? `\ndata: ${payload}` : '';
        controller.enqueue(
          encoder.encode(
            `event: error${dataLine}\n\nevent: results\ndata: {"results":[{"title":"After error"}]}\n\n`,
          ),
        );
      },
      cancel(reason) {
        cancelReason = reason;
      },
    });

    await expect(
      readLibrarrSearchStream(new Response(stream), (books) => {
        batches.push(books.map((book) => String(book.title)));
      }),
    ).rejects.toThrow(expectedMessage);

    expect(batches).toEqual([['Before error']]);
    expect(cancelReason).toBeInstanceOf(Error);
    expect((cancelReason as Error).message).toBe(expectedMessage);
  });

  it('parses empty error frames and extracts message, error, or detail', () => {
    expect(parseFrame('event: error')).toEqual({ event: 'error', data: '' });
    expect(parseStreamErrorMessage('{"message":"Message text"}')).toBe('Message text');
    expect(parseStreamErrorMessage('{"error":"Error text"}')).toBe('Error text');
    expect(parseStreamErrorMessage('{"detail":"Detail text"}')).toBe('Detail text');
    expect(parseStreamErrorMessage('Plain text stream failure')).toBe('Plain text stream failure');
    expect(parseStreamErrorMessage('{"broken":')).toBe('Librarr search failed.');
  });
});

describe('Librarr modal state helpers', () => {
  it('marks incomplete streams as error with distinct messages for zero and partial matches', () => {
    const emptyIncomplete = finalizeLibrarrStreamState(false, 0);
    const singlePartial = finalizeLibrarrStreamState(false, 1);
    const multiPartial = finalizeLibrarrStreamState(false, 3);
    const completed = finalizeLibrarrStreamState(true, 0);

    expect(emptyIncomplete).toEqual({
      state: 'error',
      error: 'Librarr ended the stream before search finished. No results were received.',
    });
    expect(singlePartial).toEqual({
      state: 'error',
      error: 'Librarr ended the stream before search finished. Showing 1 result received so far.',
    });
    expect(multiPartial).toEqual({
      state: 'error',
      error: 'Librarr ended the stream before search finished. Showing 3 results received so far.',
    });
    expect(completed).toEqual({ state: 'ready', error: '' });
  });

  it('hides the empty banner whenever an error is present or results exist', () => {
    expect(shouldShowLibrarrEmptyBanner('ready', 0, '')).toBe(true);
    expect(shouldShowLibrarrEmptyBanner('ready', 0, 'Librarr could not add that book.')).toBe(false);
    expect(shouldShowLibrarrEmptyBanner('error', 0, 'Indexer timed out.')).toBe(false);
    expect(shouldShowLibrarrEmptyBanner('ready', 2, '')).toBe(false);
  });

  it('pins result format labels and download payloads to the searched format across selector switches', () => {
    const searchedMediaType = 'audiobook' as const;
    const unlabelledResult = { id: 'book-1', title: 'The Left Hand of Darkness' };
    const explicitEbookResult = { id: 'book-2', title: 'Dune', format: 'epub' };

    expect(resolveLibrarrResultFormat(unlabelledResult, searchedMediaType)).toBe('audiobook');
    expect(resolveLibrarrResultFormat(explicitEbookResult, searchedMediaType)).toBe('epub');
    expect(buildLibrarrDownloadPayload(searchedMediaType, unlabelledResult, 42)).toEqual({
      media_type: 'audiobook',
      candidate_id: 42,
      result: unlabelledResult,
    });
    expect(
      buildLibrarrDownloadPayload(searchedMediaType, unlabelledResult, 42, '  run_abcd1234  '),
    ).toEqual({
      media_type: 'audiobook',
      candidate_id: 42,
      run_id: 'run_abcd1234',
      result: unlabelledResult,
    });
    expect(buildLibrarrDownloadPayload(searchedMediaType, unlabelledResult, 42, 'short')).toEqual({
      media_type: 'audiobook',
      candidate_id: 42,
      result: unlabelledResult,
    });
  });

  it('triggers a search on format switches for valid queries and resets short queries to idle', () => {
    expect(librarrFormatChangeAction('  Ursula Le Guin  ', 'ebook')).toEqual({
      shouldSearch: true,
      searchedMediaType: 'ebook',
      state: 'searching',
    });
    expect(librarrFormatChangeAction(' a ', 'ebook')).toEqual({
      shouldSearch: false,
      searchedMediaType: 'ebook',
      state: 'idle',
    });
    expect(librarrFormatChangeAction('   ', 'audiobook')).toEqual({
      shouldSearch: false,
      searchedMediaType: 'audiobook',
      state: 'idle',
    });
  });

  it('enables inline retry only in error state when the trimmed query has at least two characters', () => {
    expect(canRetryLibrarrSearch('error', 'Dune')).toBe(true);
    expect(canRetryLibrarrSearch('error', '  ab  ')).toBe(true);
    expect(canRetryLibrarrSearch('error', ' a ')).toBe(false);
    expect(canRetryLibrarrSearch('error', '   ')).toBe(false);
    expect(canRetryLibrarrSearch('ready', 'Dune')).toBe(false);
    expect(canRetryLibrarrSearch('searching', 'Dune')).toBe(false);
    expect(canRetryLibrarrSearch('idle', 'Dune')).toBe(false);
  });
});
