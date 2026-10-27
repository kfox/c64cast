/** Pure logic behind `TransportBar.svelte`'s time display, loop-button label,
 *  loop-slot enablement and keyboard-tap gesture. */

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
 *  Returns `null` for a real pointer-driven click. */
export function keyNudgeEvents(detail: number): HoldEvent[] | null {
  if (detail !== 0) return null;
  return [{ pressed: true }, { pressed: false }];
}

/** How often a held rw/ff press is re-sent. The server drops a console hold
 *  `_HOLD_LEASE_S` (c64cast/control/transport.py) after the last press it
 *  heard, so a closed tab cannot leave a scrub running. */
export const HOLD_KEEPALIVE_MS = 500;

/** Press-and-hold for rw/ff: sends the press, repeats it every
 *  `HOLD_KEEPALIVE_MS` while held, and sends the release once. */
export class HoldKeepAlive {
  readonly #send: (verb: string, pressed: boolean) => void;
  readonly #timers = new Map<string, ReturnType<typeof setInterval>>();

  constructor(send: (verb: string, pressed: boolean) => void) {
    this.#send = send;
  }

  press(verb: string): void {
    if (this.#timers.has(verb)) return;
    this.#send(verb, true);
    this.#timers.set(
      verb,
      setInterval(() => this.#send(verb, true), HOLD_KEEPALIVE_MS),
    );
  }

  release(verb: string): void {
    const timer = this.#timers.get(verb);
    if (timer === undefined) return;
    clearInterval(timer);
    this.#timers.delete(verb);
    this.#send(verb, false);
  }

  releaseAll(): void {
    for (const verb of [...this.#timers.keys()]) this.release(verb);
  }
}

/** The parts of a `PointerEvent` that `pointerHold` reads. */
export interface PointerHoldEvent {
  type: string;
  pointerId: number;
  currentTarget: { setPointerCapture(pointerId: number): void } | null;
}

/** One pointer event on a ◀◀/▶▶ button, applied to `holds`. A press captures
 *  the pointer: without it, a drag off the button before letting go sends its
 *  pointerup elsewhere and the keep-alive renews the hold forever. A release
 *  is never gated on `readOnly`, so a hold always lets go. */
export function pointerHold(
  holds: HoldKeepAlive,
  verb: string,
  event: PointerHoldEvent,
  readOnly: boolean,
): void {
  if (event.type === "pointerdown") {
    if (readOnly) return;
    event.currentTarget?.setPointerCapture(event.pointerId);
    holds.press(verb);
  } else {
    holds.release(verb);
  }
}
