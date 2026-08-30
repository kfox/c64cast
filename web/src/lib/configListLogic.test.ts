import { describe, expect, it } from "vitest";

import { displayLabel, refDisplayLabel, visibleRows } from "./configListLogic";
import type { ConfigFile } from "./types";

function file(overrides: Partial<ConfigFile> & Pick<ConfigFile, "path" | "rel">): ConfigFile {
  return {
    root: "shows",
    name: overrides.rel.split("/").pop() ?? overrides.rel,
    size: 0,
    mtime: 0,
    readonly: false,
    ...overrides,
  };
}

describe("displayLabel", () => {
  it("strips the .toml suffix", () => {
    expect(displayLabel(file({ path: "shows/gig.toml", rel: "gig.toml" }))).toBe("gig");
  });

  it("is case-insensitive about the suffix", () => {
    expect(displayLabel(file({ path: "shows/gig.TOML", rel: "gig.TOML" }))).toBe("gig");
  });

  it("keeps subdirectories", () => {
    expect(displayLabel(file({ path: "shows/sets/opener.toml", rel: "sets/opener.toml" }))).toBe(
      "sets/opener",
    );
  });
});

describe("refDisplayLabel", () => {
  it("drops the root label and the .toml suffix", () => {
    expect(refDisplayLabel("c64cast/config/journey.toml")).toBe("config/journey");
  });

  it("agrees with displayLabel for the same file", () => {
    const f = file({ path: "shows/sets/opener.toml", rel: "sets/opener.toml" });
    expect(refDisplayLabel(f.path)).toBe(displayLabel(f));
  });

  it("handles a ref with no slash", () => {
    expect(refDisplayLabel("gig.toml")).toBe("gig");
  });

  it("is case-insensitive about the suffix", () => {
    expect(refDisplayLabel("shows/gig.TOML")).toBe("gig");
  });
});

describe("visibleRows", () => {
  const shows = file({ path: "shows/gig.toml", rel: "gig.toml", mtime: 100 });
  const opener = file({ path: "shows/sets/opener.toml", rel: "sets/opener.toml", mtime: 300 });
  const example = file({
    path: "examples/hello.toml",
    rel: "hello.toml",
    mtime: 200,
    readonly: true,
  });
  const files = [shows, opener, example];

  it("hides read-only examples by default", () => {
    expect(visibleRows(files).map((f) => f.path)).toEqual(["shows/gig.toml", "shows/sets/opener.toml"]);
  });

  it("includes examples when asked", () => {
    expect(visibleRows(files, { showExamples: true })).toHaveLength(3);
  });

  it("filters by a case-insensitive substring of the display label", () => {
    expect(visibleRows(files, { query: "OPEN" }).map((f) => f.path)).toEqual([
      "shows/sets/opener.toml",
    ]);
  });

  it("an empty query matches everything visible", () => {
    expect(visibleRows(files, { query: "   " })).toHaveLength(2);
  });

  it("sorts by name ascending by default", () => {
    expect(visibleRows(files).map((f) => f.path)).toEqual([
      "shows/gig.toml",
      "shows/sets/opener.toml",
    ]);
  });

  it("sorts by name descending", () => {
    expect(visibleRows(files, { sort: "name-desc" }).map((f) => f.path)).toEqual([
      "shows/sets/opener.toml",
      "shows/gig.toml",
    ]);
  });

  it("sorts newest first", () => {
    expect(visibleRows(files, { sort: "newest", showExamples: true }).map((f) => f.path)).toEqual([
      "shows/sets/opener.toml",
      "examples/hello.toml",
      "shows/gig.toml",
    ]);
  });

  it("sorts oldest first", () => {
    expect(visibleRows(files, { sort: "oldest" }).map((f) => f.path)).toEqual([
      "shows/gig.toml",
      "shows/sets/opener.toml",
    ]);
  });

  it("never mutates the input array", () => {
    const copy = [...files];
    visibleRows(files, { sort: "name-desc" });
    expect(files).toEqual(copy);
  });
});
