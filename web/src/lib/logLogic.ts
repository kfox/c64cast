import type { LogLine } from "./types";

/**
 * The browser's log after one frame's `log` arrives.
 *
 * The daemon follows the log per socket: a connection's first frame carries
 * the backlog, and every later frame only what that connection has not been
 * sent. So the first frame on a new socket replaces what the page holds — the
 * backlog overlaps it, and after a daemon restart its sequence numbers start
 * over — and only the frames after it append.
 */
export function mergeLog(
  held: LogLine[],
  incoming: LogLine[],
  firstOnSocket: boolean,
  limit: number,
): LogLine[] {
  const merged = firstOnSocket ? incoming : held.concat(incoming);
  return merged.length > limit ? merged.slice(merged.length - limit) : merged;
}
