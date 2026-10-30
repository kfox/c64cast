import { describe, expect, it } from "vitest";

import { mergeLog } from "./logLogic";
import type { LogLine } from "./types";

function line(seq: number): LogLine {
  return { seq, t: seq, level: "INFO", name: "c64cast", message: `line ${seq}`, generation: 0 };
}

function seqs(lines: LogLine[]): number[] {
  return lines.map((l) => l.seq);
}

describe("mergeLog", () => {
  it("appends what a connection sends after its first frame", () => {
    expect(seqs(mergeLog([line(1), line(2)], [line(3)], false, 500))).toEqual([1, 2, 3]);
  });

  it("does not repeat the backlog a reconnected socket is sent", () => {
    const held = [line(1), line(2), line(3)];
    const backlog = [line(2), line(3), line(4)];
    expect(seqs(mergeLog(held, backlog, true, 500))).toEqual([2, 3, 4]);
  });

  it("drops a restarted daemon's predecessor's lines", () => {
    const held = [line(7), line(8)];
    expect(seqs(mergeLog(held, [], true, 500))).toEqual([]);
  });

  it("keeps only the newest lines past the limit", () => {
    expect(seqs(mergeLog([line(1), line(2)], [line(3)], false, 2))).toEqual([2, 3]);
  });
});
