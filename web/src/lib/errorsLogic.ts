/** The status-code-to-sentence mapping every screen needs when a request
 *  fails — pulled out of `Session.svelte` and `Config.svelte`, which each grew
 *  their own copy and drifted (Config's was missing the 422 case, the one
 *  that matters when a start or a save is refused for a bad config). */

import { ApiError } from "./api";

/** Turn a caught error into the sentence a screen's `problem` line shows. */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return `Not allowed: ${e.message}`;
    if (e.status === 404) return `No such configuration: ${e.message}`;
    if (e.status === 409) return `Can't do that right now: ${e.message}`;
    if (e.status === 413) return `That file is too large to edit here: ${e.message}`;
    if (e.status === 422) return `That configuration will not run: ${e.message}`;
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
