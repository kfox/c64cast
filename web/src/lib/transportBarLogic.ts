/** Pure logic behind `TransportBar.svelte`'s time display, loop-button label,
 *  loop-slot enablement and keyboard-tap gesture — pulled out of the
 *  component so it can be unit-tested without mounting Svelte. */

import type { LoopState } from "./types";

/** `M:SS`, floor-clamped at zero and rounded to the nearest second. */
export function formatTime(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** The loop button's label for each A/B loop state. */
export function loopButtonLabel(state: LoopState["state"]): string {
  if (state === "none") return "Mark loop A";
  if (state === "armed") return "Mark loop B";
  return "Clear loop";
}

/** Whether loop-slot pad `slot` is clickable: a pad already holding a saved
 *  loop always recalls it, and any pad accepts a save while SAVE is armed. */
export function slotEnabled(saved: boolean, savingSlot: boolean): boolean {
  return saved || savingSlot;
}

export interface HoldEvent {
  pressed: boolean;
}

/** A keyboard-synthesized click (Enter/Space on a focused button) reports
 *  `detail === 0` — there was no pointerdown/up to hold against, so it becomes
 *  a tap: one pressed event immediately followed by one released event.
 *  Returns `null` for a real pointer-driven click, which the hold handlers
 *  already cover. */
export function keyNudgeEvents(detail: number): HoldEvent[] | null {
  if (detail !== 0) return null;
  return [{ pressed: true }, { pressed: false }];
}
