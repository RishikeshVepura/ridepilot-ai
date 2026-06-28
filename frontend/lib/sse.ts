/**
 * Incremental Server-Sent Events parsing for `fetch` response bodies.
 *
 * The AI Service replies to POST /api/chat/message with a `text/event-stream`
 * body rather than a JSON document, so we cannot use `EventSource` (which only
 * does GET) nor `response.json()`. Instead we read the `ReadableStream` chunk by
 * chunk, buffer partial frames across chunk boundaries, and yield each complete
 * event's parsed `data:` payload as it arrives. This is what lets assistant
 * tokens render live as they stream.
 *
 * SSE wire format we handle (per the AI Service, which emits `data: <json>\n\n`):
 *   - Events are separated by a blank line (\n\n, tolerant of \r\n\r\n).
 *   - Within an event, one or more `data:` lines carry the payload; per spec
 *     multiple data lines are joined with a newline.
 *   - Lines beginning with `:` are comments (keepalives) and are ignored.
 */

/**
 * Parse the raw text of one SSE event block into its concatenated `data:`
 * payload, or null if the block carries no data lines (e.g. a comment-only
 * keepalive block).
 */
function extractData(block: string): string | null {
  const dataLines: string[] = [];
  for (const rawLine of block.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (line.startsWith(":")) {
      // Comment / keepalive line — ignore.
      continue;
    }
    if (line.startsWith("data:")) {
      // Strip the field name and a single optional leading space.
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    // Other SSE fields (event:, id:, retry:) are not used by this backend.
  }
  if (dataLines.length === 0) {
    return null;
  }
  return dataLines.join("\n");
}

/**
 * Read an SSE response body and yield each event's parsed JSON payload.
 *
 * Generic over the payload type so callers can narrow to their event union.
 * Frames that are not valid JSON are skipped rather than throwing, so a single
 * malformed frame cannot abort the whole stream.
 *
 * @param body The `ReadableStream` from a `fetch` Response (response.body).
 * @param signal Optional AbortSignal; when aborted, reading stops promptly.
 */
export async function* parseSSEStream<T = unknown>(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<T, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) {
        break;
      }
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      // Split off every complete event (terminated by a blank line). Tolerate
      // both \n\n and \r\n\r\n separators; keep the trailing partial in buffer.
      let separatorIndex = findSeparator(buffer);
      while (separatorIndex !== -1) {
        const block = buffer.slice(0, separatorIndex.index);
        buffer = buffer.slice(separatorIndex.index + separatorIndex.length);

        const data = extractData(block);
        if (data !== null) {
          const parsed = tryParseJson<T>(data);
          if (parsed !== undefined) {
            yield parsed;
          }
        }
        separatorIndex = findSeparator(buffer);
      }
    }

    // Flush any trailing complete event left in the buffer at stream end.
    const tail = buffer.trim();
    if (tail.length > 0) {
      const data = extractData(tail);
      if (data !== null) {
        const parsed = tryParseJson<T>(data);
        if (parsed !== undefined) {
          yield parsed;
        }
      }
    }
  } finally {
    // Releasing the lock lets the underlying connection be torn down cleanly
    // (important when the consumer aborts mid-stream).
    reader.releaseLock();
  }
}

/** Locate the next event separator (blank line) in the buffer. */
function findSeparator(
  buffer: string,
): { index: number; length: number } | -1 {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) {
    return -1;
  }
  if (crlf === -1 || (lf !== -1 && lf < crlf)) {
    return { index: lf, length: 2 };
  }
  return { index: crlf, length: 4 };
}

/** Parse JSON, returning undefined (rather than throwing) on malformed input. */
function tryParseJson<T>(text: string): T | undefined {
  try {
    return JSON.parse(text) as T;
  } catch {
    return undefined;
  }
}
