import { describe, expect, it } from "vitest";

import {
  commandForKey,
  commandForKeyUp,
  isTypingTarget,
  type LiveKeyState,
} from "./liveKeysLogic";
import type { Clip } from "./types";

function clip(slot: number): Clip {
  return {
    slot,
    name: `clip ${slot}`,
    type: null,
    pad: null,
    pad_type: "",
    launch: "trigger",
    quantize: "beat",
    loop: false,
    state: "loaded",
  };
}

function state(overrides: Partial<LiveKeyState> = {}): LiveKeyState {
  return {
    readOnly: false,
    paused: false,
    videoFrozen: false,
    clips: [clip(1), clip(2), clip(3)],
    ...overrides,
  };
}

describe("isTypingTarget", () => {
  it("is true for the form-control tags", () => {
    expect(isTypingTarget("INPUT", false)).toBe(true);
    expect(isTypingTarget("textarea", false)).toBe(true);
    expect(isTypingTarget("Select", false)).toBe(true);
    expect(isTypingTarget("BUTTON", false)).toBe(true);
  });

  it("is true for anything contenteditable, whatever the tag", () => {
    expect(isTypingTarget("DIV", true)).toBe(true);
  });

  it("is false for an ordinary element", () => {
    expect(isTypingTarget("DIV", false)).toBe(false);
    expect(isTypingTarget("BODY", false)).toBe(false);
  });
});

describe("commandForKey", () => {
  it("maps Space to pause when running", () => {
    expect(commandForKey(" ", false, state({ paused: false }))).toEqual([
      { action: "transport", verb: "pause" },
    ]);
  });

  it("maps Space to resume when paused", () => {
    expect(commandForKey(" ", false, state({ paused: true }))).toEqual([
      { action: "transport", verb: "resume" },
    ]);
  });

  it("maps t to tap", () => {
    expect(commandForKey("t", false, state())).toEqual([{ action: "tap" }]);
  });

  it("maps n to skip", () => {
    expect(commandForKey("n", false, state())).toEqual([
      { action: "transport", verb: "skip" },
    ]);
  });

  it("maps f to freeze when not frozen", () => {
    expect(commandForKey("f", false, state({ videoFrozen: false }))).toEqual([
      { action: "transport", verb: "freeze" },
    ]);
  });

  it("maps f to unfreeze when frozen", () => {
    expect(commandForKey("f", false, state({ videoFrozen: true }))).toEqual([
      { action: "transport", verb: "unfreeze" },
    ]);
  });

  it("f does nothing for a scene with no transport", () => {
    expect(commandForKey("f", false, state({ videoFrozen: null }))).toBeNull();
  });

  it("maps l to loop_toggle", () => {
    expect(commandForKey("l", false, state())).toEqual([
      { action: "transport", verb: "loop_toggle" },
    ]);
  });

  it("l does nothing for a scene with no transport", () => {
    expect(commandForKey("l", false, state({ videoFrozen: null }))).toBeNull();
  });

  it("maps an uppercase letter the same as lowercase (Caps Lock)", () => {
    expect(commandForKey("T", false, state())).toEqual([{ action: "tap" }]);
  });

  it("maps [ to a rewind press", () => {
    expect(commandForKey("[", false, state())).toEqual([
      { action: "transport", verb: "rw", pressed: true },
    ]);
  });

  it("maps ] to a fast-forward press", () => {
    expect(commandForKey("]", false, state())).toEqual([
      { action: "transport", verb: "ff", pressed: true },
    ]);
  });

  it("maps a digit to a press-and-release launch of that slot", () => {
    expect(commandForKey("2", false, state())).toEqual([
      { action: "launch", slot: 2, pressed: true },
      { action: "launch", slot: 2, pressed: false },
    ]);
  });

  it("a digit with no clip in that slot does nothing", () => {
    expect(commandForKey("7", false, state())).toBeNull();
  });

  it("? is not a command", () => {
    expect(commandForKey("?", false, state())).toBeNull();
  });

  it("an unrelated key does nothing", () => {
    expect(commandForKey("Escape", false, state())).toBeNull();
  });

  it("a modifier maps everything to nothing", () => {
    expect(commandForKey("t", true, state())).toBeNull();
    expect(commandForKey(" ", true, state())).toBeNull();
    expect(commandForKey("1", true, state())).toBeNull();
  });

  it("a read-only console maps everything to nothing", () => {
    expect(commandForKey("t", false, state({ readOnly: true }))).toBeNull();
  });
});

describe("commandForKeyUp", () => {
  it("releases a held rewind", () => {
    expect(commandForKeyUp("[")).toEqual([{ action: "transport", verb: "rw", pressed: false }]);
  });

  it("releases a held fast-forward", () => {
    expect(commandForKeyUp("]")).toEqual([{ action: "transport", verb: "ff", pressed: false }]);
  });

  it("does nothing for a key with no held state", () => {
    expect(commandForKeyUp("t")).toBeNull();
  });
});
