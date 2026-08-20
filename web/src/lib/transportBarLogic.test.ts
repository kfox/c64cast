import { describe, expect, it } from "vitest";

import { formatTime, keyNudgeEvents, loopButtonLabel, slotEnabled } from "./transportBarLogic";

describe("formatTime", () => {
  it("formats whole minutes and seconds", () => {
    expect(formatTime(125)).toBe("2:05");
  });

  it("pads single-digit seconds", () => {
    expect(formatTime(61)).toBe("1:01");
  });

  it("rounds to the nearest second", () => {
    expect(formatTime(59.6)).toBe("1:00");
  });

  it("clamps negative values at zero", () => {
    expect(formatTime(-5)).toBe("0:00");
  });

  it("handles zero", () => {
    expect(formatTime(0)).toBe("0:00");
  });
});

describe("loopButtonLabel", () => {
  it("invites marking A when no loop is set", () => {
    expect(loopButtonLabel("none")).toBe("Mark loop A");
  });

  it("invites marking B once A is armed", () => {
    expect(loopButtonLabel("armed")).toBe("Mark loop B");
  });

  it("offers to clear an active loop", () => {
    expect(loopButtonLabel("active")).toBe("Clear loop");
  });
});

describe("slotEnabled", () => {
  it("a saved pad is always clickable", () => {
    expect(slotEnabled(true, false)).toBe(true);
    expect(slotEnabled(true, true)).toBe(true);
  });

  it("an empty pad is only clickable while SAVE is armed", () => {
    expect(slotEnabled(false, false)).toBe(false);
    expect(slotEnabled(false, true)).toBe(true);
  });
});

describe("keyNudgeEvents", () => {
  it("a keyboard click (detail 0) becomes a press immediately followed by a release", () => {
    expect(keyNudgeEvents(0)).toEqual([{ pressed: true }, { pressed: false }]);
  });

  it("a real pointer click (detail >= 1) yields nothing — the hold handlers cover it", () => {
    expect(keyNudgeEvents(1)).toBeNull();
  });
});
