/** Press-and-release pointer handling shared by the console's hold buttons:
 *  `TransportBar`'s ◀◀/▶▶ and `ClipGrid`'s pads. */

/** The parts of a `PointerEvent` that `pointerHold` reads. */
export interface PointerHoldEvent {
  type: string;
  pointerId: number;
  currentTarget: { setPointerCapture(pointerId: number): void } | null;
}

/** What a hold button drives: one press when it goes down, one release when
 *  it comes up. Both must be safe to repeat, because a release arrives twice
 *  (`pointerup`, then the `lostpointercapture` that follows it). */
export interface Holds<K> {
  press(key: K): void;
  release(key: K): void;
}

/** One pointer event on a hold button, applied to `holds`. A press captures
 *  the pointer: without it, a mouse dragged off the button before letting go
 *  sends its pointerup elsewhere and the hold never ends. A release is never
 *  gated on `readOnly`, so a hold always lets go. */
export function pointerHold<K>(
  holds: Holds<K>,
  key: K,
  event: PointerHoldEvent,
  readOnly: boolean,
): void {
  if (event.type === "pointerdown") {
    if (readOnly) return;
    event.currentTarget?.setPointerCapture(event.pointerId);
    holds.press(key);
  } else {
    holds.release(key);
  }
}

/** `Holds` for a button whose press and release each go out once per gesture,
 *  however many events report them. */
export class HeldKeys<K> implements Holds<K> {
  readonly #send: (key: K, pressed: boolean) => void;
  readonly #held = new Set<K>();

  constructor(send: (key: K, pressed: boolean) => void) {
    this.#send = send;
  }

  press(key: K): void {
    if (this.#held.has(key)) return;
    this.#held.add(key);
    this.#send(key, true);
  }

  release(key: K): void {
    if (!this.#held.delete(key)) return;
    this.#send(key, false);
  }
}
