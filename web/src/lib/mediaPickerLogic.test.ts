import { describe, expect, it } from "vitest";

import { kindsForScene, pickerOptions, urlFromDrop } from "./mediaPickerLogic";
import type { MediaEntry, SceneTypeDoc } from "./types";

function entry(overrides: Partial<MediaEntry> & Pick<MediaEntry, "spec">): MediaEntry {
  return {
    name: overrides.spec.split("/").pop() ?? overrides.spec,
    is_dir: false,
    size: 0,
    mtime: 0,
    ...overrides,
  };
}

describe("pickerOptions", () => {
  it("lists directories before files, each alphabetical", () => {
    const entries = [
      entry({ spec: "assets/videos/b.mp4" }),
      entry({ spec: "assets/videos/more", is_dir: true }),
      entry({ spec: "assets/videos/a.mp4" }),
      entry({ spec: "assets/videos", is_dir: true }),
    ];
    expect(pickerOptions(entries)).toEqual([
      "assets/videos",
      "assets/videos/more",
      "assets/videos/a.mp4",
      "assets/videos/b.mp4",
    ]);
  });

  it("does not mutate its input", () => {
    const entries = [entry({ spec: "b" }), entry({ spec: "a" })];
    const copy = [...entries];
    pickerOptions(entries);
    expect(entries).toEqual(copy);
  });

  it("is empty for no entries", () => {
    expect(pickerOptions([])).toEqual([]);
  });
});

describe("kindsForScene", () => {
  function sceneType(media_kinds: string[]): SceneTypeDoc {
    return { name: "video", help: "", displays: [], fields: [], media_kinds };
  }

  it("returns the scene type's media kinds", () => {
    expect(kindsForScene(sceneType(["video"]))).toEqual(["video"]);
  });

  it("is empty for a type with no file field", () => {
    expect(kindsForScene(sceneType([]))).toEqual([]);
  });

  it("is empty when the scene type isn't known yet", () => {
    expect(kindsForScene(undefined)).toEqual([]);
  });
});

describe("urlFromDrop", () => {
  it("reads a URL from text/uri-list", () => {
    expect(urlFromDrop({ "text/uri-list": "https://example.com/clip.mp4" })).toBe(
      "https://example.com/clip.mp4",
    );
  });

  it("falls back to text/plain", () => {
    expect(urlFromDrop({ "text/plain": "http://example.com/clip.mp4" })).toBe(
      "http://example.com/clip.mp4",
    );
  });

  it("skips a uri-list comment line to find the URL", () => {
    const payload = { "text/uri-list": "# a comment\nhttps://example.com/clip.mp4" };
    expect(urlFromDrop(payload)).toBe("https://example.com/clip.mp4");
  });

  it("rejects a file:// path", () => {
    expect(urlFromDrop({ "text/uri-list": "file:///Users/me/clip.mp4" })).toBeNull();
  });

  it("rejects a bare filename (an upload, not a URL)", () => {
    expect(urlFromDrop({ "text/plain": "clip.mp4" })).toBeNull();
  });

  it("is null with nothing dropped", () => {
    expect(urlFromDrop({})).toBeNull();
  });
});
