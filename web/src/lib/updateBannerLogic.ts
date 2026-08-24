/**
 * Pure logic behind `UpdateBanner.svelte`, split out the same way
 * `configListLogic.ts`/`transportBarLogic.ts` are — so it can be unit-tested
 * without a component-testing library, which this project doesn't carry.
 */

import type { UpdateState } from "./types";

export const DISMISSED_KEY = "c64cast.update-dismissed-version";
export const STALE_DISMISSED_KEY = "c64cast.stale-dismissed-version";

const MS_PER_DAY = 24 * 60 * 60 * 1000;

/** The minimal shape `UpdateBanner.svelte` needs from `localStorage` —
 *  `Storage`'s own two methods, injectable so a test can pass a plain object
 *  instead of a DOM global these tests don't have. */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Whether `state` names a release worth showing a banner for at all — a
 *  completed check, that found something newer, naming what it found. */
export function isPending(state: UpdateState | null): boolean {
  return state?.checked === true && state.newer === true && !!state.latest_version;
}

/** When this host last *learned* anything from PyPI, in epoch ms — the
 *  moment its silence began if it is currently silent, else the last attempt,
 *  which answered. `checked_at` alone cannot say: an appliance offline for a
 *  year still bumps it daily as its timer fails and its last real answer
 *  rides along untouched (`update_state.record_check`). */
export function lastAnsweredAt(state: UpdateState): number | null {
  const seconds = state.unanswered_since ?? state.checked_at;
  return seconds === undefined || seconds === null ? null : seconds * 1000;
}

/** Whether this host has gone long enough without an answer that whatever
 *  the banner would otherwise claim about being current is too old to trust.
 *  How long is the host's own `stale_after_days` (`update_state.py` owns the
 *  number, and `c64cast --motd-line` prints from the same one); a response
 *  that doesn't say is never called stale. `now` is injected rather than
 *  read from the clock so this stays a pure function. */
export function isStale(state: UpdateState | null, now: number): boolean {
  if (state?.checked !== true || !state.stale_after_days) return false;
  const answered = lastAnsweredAt(state);
  return answered !== null && now - answered > state.stale_after_days * MS_PER_DAY;
}

/** Whether the pending release in `state` is the one this browser already
 *  dismissed. Per-browser, not server-side: which version a browser has seen
 *  is not something another console watching the same host needs to agree
 *  with — see `api.ts`'s note on `library()` for the shared-state cases that
 *  do belong on the server instead of here. */
export function isDismissed(state: UpdateState | null, dismissedVersion: string | null): boolean {
  return !!state?.latest_version && dismissedVersion === state.latest_version;
}

/** This browser's `localStorage`, or null where there isn't one to have.
 *  Reading the global *itself* throws when a browser is set to block site
 *  data, so the access has to be guarded and not just the calls on it —
 *  otherwise a blocked browser takes the whole console down over a banner. */
export function browserStore(): KeyValueStore | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Whether this browser has already dismissed the staleness reminder for the
 *  release it is running. Dismissed *until the next upgrade* rather than for
 *  a fixed spell: a box with no internet cannot make the reminder stop being
 *  true, so the useful "I know, leave me alone" lasts exactly as long as the
 *  install it was said about. */
export function isStaleDismissed(
  state: UpdateState | null,
  dismissedVersion: string | null,
): boolean {
  return !!state?.running_version && dismissedVersion === state.running_version;
}

/** The value this browser last dismissed under `key`, or null — for no
 *  store, no record, or a read that failed. */
export function readDismissed(storage: KeyValueStore | null, key: string): string | null {
  if (storage === null) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

/** Record `value` as dismissed under `key`. A write that fails (no store, a
 *  full quota) just means the banner reappears next load — worth living with
 *  rather than surfacing a failure over a dismiss click. */
export function writeDismissed(storage: KeyValueStore | null, key: string, value: string): void {
  if (storage === null) return;
  try {
    storage.setItem(key, value);
  } catch {
    // See above.
  }
}
