/** Pure logic behind Live's keyboard shortcuts — pulled out of the component
 *  so the key→command mapping can be unit-tested without mounting Svelte.
 *  Follows `transportBarLogic.keyNudgeEvents`'s precedent of keeping the
 *  wire-shape decisions here and the DOM plumbing (`<svelte:window>`,
 *  `event.repeat`) in the component. */

import type { Clip } from "./types";

/** One `{action: …}` frame `Console.send` takes, minus `system` — the same
 *  shape every panel on Live already builds and hands to its own `send()`
 *  wrapper, which is what adds `system`. */
export type LiveCommand = Record<string, unknown>;

export interface LiveKeyState {
  /** Every control on Live is dead in this state (`host.readOnly ||
   *  !host.connected`) — the shortcut layer must never be a way around it. */
  readOnly: boolean;
  /** A machine-level halt (the C64's own keys, MIDI, `/perf`) — what Space
   *  toggles. Distinct from `videoFrozen`. */
  paused: boolean;
  /** The current scene's DJ-transport freeze state, or `null` for a scene
   *  with no transport (a generator or a picture) — `f`/`l` do nothing then,
   *  the same as the transport panel not rendering for one. */
  videoFrozen: boolean | null;
  /** The clip grid, so a digit key only fires for a slot that actually
   *  exists — matching `ClipGrid`, which renders (and so can launch) exactly
   *  the slots the running config declares. */
  clips: Clip[];
}

const DIGIT_SLOTS = new Set([1, 2, 3, 4, 5, 6, 7, 8]);
const TYPING_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT", "BUTTON"]);

/** Bail on INPUT/TEXTAREA/SELECT/BUTTON and anything editable. `BUTTON`
 *  matters as much as the text fields: `ClipGrid` and `TransportBar` detect a
 *  keyboard-synthesized click via `event.detail === 0`, so a global handler
 *  that also acted on the same keystroke (Space, most of all) would either
 *  double-fire or — if it called `preventDefault()` — silently break launching
 *  a clip from the keyboard. */
export function isTypingTarget(tagName: string, isContentEditable: boolean): boolean {
  return isContentEditable || TYPING_TAGS.has(tagName.toUpperCase());
}

/** The command(s) a keydown fires, or `null` for a key this layer doesn't
 *  own, a held modifier, or a read-only console. `hasModifier` is Ctrl/Alt/Meta
 *  only — Shift is deliberately excluded, because a plain `?` is `Shift+/` on
 *  a US layout and blocking it would make the help toggle unreachable, and
 *  Caps Lock reports the same uppercase letters Shift would without either
 *  meaning "this is a browser shortcut". `?` is deliberately not a command
 *  here — it toggles the on-screen shortcut list, which is local UI state the
 *  component owns directly. A clip launch sends a press *and* a release
 *  together, the same tap `ClipGrid`'s own keyboard path sends for a focused
 *  pad; `[`/`]` send only the press, and `commandForKeyUp` sends their
 *  release once the key comes back up. */
export function commandForKey(
  key: string,
  hasModifier: boolean,
  state: LiveKeyState,
): LiveCommand[] | null {
  if (hasModifier || state.readOnly) return null;

  switch (key) {
    case " ":
      return [{ action: "transport", verb: state.paused ? "resume" : "pause" }];
    case "t":
    case "T":
      return [{ action: "tap" }];
    case "n":
    case "N":
      return [{ action: "transport", verb: "skip" }];
    case "f":
    case "F":
      if (state.videoFrozen === null) return null;
      return [{ action: "transport", verb: state.videoFrozen ? "unfreeze" : "freeze" }];
    case "l":
    case "L":
      if (state.videoFrozen === null) return null;
      return [{ action: "transport", verb: "loop_toggle" }];
    case "[":
      return [{ action: "transport", verb: "rw", pressed: true }];
    case "]":
      return [{ action: "transport", verb: "ff", pressed: true }];
    default: {
      const slot = Number(key);
      if (!DIGIT_SLOTS.has(slot) || !state.clips.some((c) => c.slot === slot)) return null;
      return [
        { action: "launch", slot, pressed: true },
        { action: "launch", slot, pressed: false },
      ];
    }
  }
}

/** The release half of a held key — only `[`/`]` have one; every other key's
 *  command is already complete on keydown. */
export function commandForKeyUp(key: string): LiveCommand[] | null {
  if (key === "[") return [{ action: "transport", verb: "rw", pressed: false }];
  if (key === "]") return [{ action: "transport", verb: "ff", pressed: false }];
  return null;
}
