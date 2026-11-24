export type LibrarrResult = Record<string, unknown>;
export type LibrarrMediaType = 'ebook' | 'audiobook';
export type LibrarrSearchState = 'idle' | 'searching' | 'ready' | 'error';

function isRecord(value: unknown): value is LibrarrResult {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function extractResults(payload: unknown): LibrarrResult[] {
  let values: unknown = payload;
  if (isRecord(payload)) {
    for (const key of ['results', 'items', 'books', 'data']) {
      if (key in payload) {
        values = payload[key];
        break;
      }
    }
  }
  if (!Array.isArray(values)) return [];
  return values
    .slice(0, 50)
    .filter(isRecord)
    .filter((result) => {
      try {
        return JSON.stringify(result).length <= 25_000;
      } catch {
        return false;
      }
    });
}

export function parseFrame(frame: string): { event: string; data: string } | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of frame.split(/\r\n|\r|\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  return data.length || event === 'complete' || event === 'error'
    ? { event, data: data.join('\n') }
    : null;
}

export function parseStreamErrorMessage(data: string): string {
  const trimmed = data.trim();
  if (!trimmed) return 'Librarr search failed.';
  try {
    const payload: unknown = JSON.parse(trimmed);
    if (typeof payload === 'string' && payload.trim()) return payload.trim();
    if (isRecord(payload)) {
      for (const key of ['message', 'error', 'detail']) {
        const value = payload[key];
        if (typeof value === 'string' && value.trim()) return value.trim();
      }
    }
    return 'Librarr search failed.';
  } catch {
    return trimmed.startsWith('{') || trimmed.startsWith('[')
      ? 'Librarr search failed.'
      : trimmed;
  }
}

export function resolveLibrarrResultFormat(
  result: LibrarrResult,
  searchedMediaType: LibrarrMediaType,
): string {
  for (const key of ['media_type', 'format', 'type']) {
    const value = result[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number') return String(value);
    if (Array.isArray(value)) {
      const values = value
        .filter((item) => typeof item === 'string' || typeof item === 'number')
        .map(String)
        .filter(Boolean);
      if (values.length) return values.join(', ');
    }
    if (value && typeof value === 'object') {
      for (const nestedKey of ['url', 'href', 'name', 'title']) {
        const nested = (value as Record<string, unknown>)[nestedKey];
        if (typeof nested === 'string' && nested.trim()) return nested.trim();
      }
    }
  }
  return searchedMediaType;
}

export function buildLibrarrDownloadPayload(
  searchedMediaType: LibrarrMediaType,
  result: LibrarrResult,
  candidateId?: number,
): { media_type: LibrarrMediaType; candidate_id: number | undefined; result: LibrarrResult } {
  return {
    media_type: searchedMediaType,
    candidate_id: candidateId,
    result,
  };
}

export function librarrFormatChangeAction(
  query: string,
  nextMediaType: LibrarrMediaType,
): {
  shouldSearch: boolean;
  searchedMediaType: LibrarrMediaType;
  state: 'idle' | 'searching';
} {
  if (query.trim().length >= 2) {
    return {
      shouldSearch: true,
      searchedMediaType: nextMediaType,
      state: 'searching',
    };
  }
  return {
    shouldSearch: false,
    searchedMediaType: nextMediaType,
    state: 'idle',
  };
}

export function incompleteLibrarrStreamMessage(resultCount: number): string {
  if (resultCount > 0) {
    return `Librarr ended the stream before search finished. Showing ${resultCount} ${resultCount === 1 ? 'result' : 'results'} received so far.`;
  }
  return 'Librarr ended the stream before search finished. No results were received.';
}

export function finalizeLibrarrStreamState(
  completed: boolean,
  resultCount: number,
): { state: 'ready' | 'error'; error: string } {
  if (completed) return { state: 'ready', error: '' };
  return {
    state: 'error',
    error: incompleteLibrarrStreamMessage(resultCount),
  };
}

export function canRetryLibrarrSearch(state: LibrarrSearchState, query: string): boolean {
  return state === 'error' && query.trim().length >= 2;
}

export function shouldShowLibrarrEmptyBanner(
  state: LibrarrSearchState,
  resultCount: number,
  error: string,
): boolean {
  return state === 'ready' && resultCount === 0 && !error;
}

/** Consume Librarr's SSE protocol, replacing results with each cumulative snapshot. */
export async function readLibrarrSearchStream(
  response: Response,
  onResults: (results: LibrarrResult[]) => void,
): Promise<{ completed: boolean }> {
  if (!response.body) throw new Error('Librarr returned an empty event stream.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;

  const consumeFrame = (frame: string) => {
    const parsed = parseFrame(frame);
    if (!parsed || !['results', 'complete', 'error'].includes(parsed.event)) return;
    if (parsed.event === 'error') {
      throw new Error(parseStreamErrorMessage(parsed.data));
    }
    let payload: unknown = null;
    if (parsed.data) {
      try {
        payload = JSON.parse(parsed.data);
      } catch {
        throw new Error('Librarr sent an invalid search update.');
      }
    }
    if (parsed.event === 'results') onResults(extractResults(payload));
    if (parsed.event === 'complete') {
      if (isRecord(payload) && ['results', 'items', 'books', 'data'].some((key) => key in payload)) {
        onResults(extractResults(payload));
      }
      completed = true;
    }
  };

  const consumeAvailableFrames = () => {
    const separator = /\r?\n\r?\n/;
    let match: RegExpExecArray | null;
    while ((match = separator.exec(buffer)) !== null) {
      consumeFrame(buffer.slice(0, match.index));
      buffer = buffer.slice(match.index + match[0].length);
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      consumeAvailableFrames();
    }
    if (buffer.trim()) consumeFrame(buffer);
    return { completed };
  } catch (error) {
    await reader.cancel(error).catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}
