import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HeldKeys, type PointerHoldEvent, pointerHold } from "./pointerHold";
import { HOLD_KEEPALIVE_MS, HoldKeepAlive } from "./transportBarLogic";

function event(type: string, captured: number[]): PointerHoldEvent {
  return { type, pointerId: 7, currentTarget: { setPointerCapture: (id) => captured.push(id) } };
}

describe("pointerHold", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function setup(): { sent: [string, boolean][]; holds: HoldKeepAlive; captured: number[] } {
    const sent: [string, boolean][] = [];
    return {
      sent,
      holds: new HoldKeepAlive((verb, pressed) => sent.push([verb, pressed])),
      captured: [],
    };
  }

  it("captures the pointer on press, so the release lands after a drag off", () => {
    const { sent, holds, captured } = setup();
    pointerHold(holds, "ff", event("pointerdown", captured), false);
    expect(captured).toEqual([7]);
    expect(sent).toEqual([["ff", true]]);
  });

  it("lets go when the capture is lost", () => {
    const { sent, holds, captured } = setup();
    pointerHold(holds, "rw", event("pointerdown", captured), false);
    pointerHold(holds, "rw", event("lostpointercapture", captured), false);
    vi.advanceTimersByTime(HOLD_KEEPALIVE_MS * 3);
    expect(sent).toEqual([
      ["rw", true],
      ["rw", false],
    ]);
  });

  it("lets go on pointerup and pointercancel, read-only or not", () => {
    for (const type of ["pointerup", "pointercancel"]) {
      const { sent, holds, captured } = setup();
      pointerHold(holds, "ff", event("pointerdown", captured), false);
      pointerHold(holds, "ff", event(type, captured), true);
      expect(sent).toEqual([
        ["ff", true],
        ["ff", false],
      ]);
    }
  });

  it("neither presses nor captures while read-only", () => {
    const { sent, holds, captured } = setup();
    pointerHold(holds, "ff", event("pointerdown", captured), true);
    expect(sent).toEqual([]);
    expect(captured).toEqual([]);
  });
});

describe("HeldKeys", () => {
  function setup(): { sent: [number, boolean][]; pads: HeldKeys<number>; captured: number[] } {
    const sent: [number, boolean][] = [];
    return {
      sent,
      pads: new HeldKeys((slot, pressed) => sent.push([slot, pressed])),
      captured: [],
    };
  }

  it("captures a pad's pointer and releases it when the capture is lost", () => {
    const { sent, pads, captured } = setup();
    pointerHold(pads, 3, event("pointerdown", captured), false);
    pointerHold(pads, 3, event("lostpointercapture", captured), false);
    expect(captured).toEqual([7]);
    expect(sent).toEqual([
      [3, true],
      [3, false],
    ]);
  });

  it("sends one release for the pointerup and the lostpointercapture after it", () => {
    const { sent, pads, captured } = setup();
    pointerHold(pads, 3, event("pointerdown", captured), false);
    pointerHold(pads, 3, event("pointerup", captured), false);
    pointerHold(pads, 3, event("lostpointercapture", captured), false);
    expect(sent).toEqual([
      [3, true],
      [3, false],
    ]);
  });

  it("sends nothing for a release with no press before it", () => {
    const { sent, pads } = setup();
    pads.release(3);
    expect(sent).toEqual([]);
  });

  it("releases every held pad once on releaseAll", () => {
    const { sent, pads, captured } = setup();
    pointerHold(pads, 3, event("pointerdown", captured), false);
    pointerHold(pads, 5, event("pointerdown", captured), false);
    pads.releaseAll();
    pads.releaseAll();
    expect(sent).toEqual([
      [3, true],
      [5, true],
      [3, false],
      [5, false],
    ]);
  });

  it("sends one press for a second pointer on a pad already down", () => {
    const { sent, pads, captured } = setup();
    pointerHold(pads, 3, event("pointerdown", captured), false);
    pointerHold(pads, 3, event("pointerdown", captured), false);
    expect(sent).toEqual([[3, true]]);
  });
});
