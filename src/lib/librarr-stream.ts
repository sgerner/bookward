export type LibrarrResult = Record<string, unknown>;

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

function parseFrame(frame: string): { event: string; data: string } | null {
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
  return data.length || event === 'complete' ? { event, data: data.join('\n') } : null;
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
    if (!parsed || !['results', 'complete'].includes(parsed.event)) return;
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
