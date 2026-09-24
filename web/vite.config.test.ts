import { fileURLToPath } from "node:url";
import { loadConfigFromFile } from "vite";
import { describe, expect, it } from "vitest";

// `build.target` is the console's browser floor (see README.md). Left unstated
// it is Vite's `baseline-widely-available` default, which resolves to a later
// set of browsers with each Vite version — and deleting the stated value moves
// no byte of the bundle for as long as that default still matches it, so the
// committed-bundle diff is green either way and this is the only check that
// fails.
//
// `loadConfigFromFile` returns the config as the file writes it, before Vite
// applies any default, which is what makes "states none" distinguishable from
// "states today's default".

// A name carrying the version it is supported from, as esbuild and Lightning
// CSS spell a target: chrome111, safari16.4, ios16.4. What it is here to
// reject is a target that names no version and so resolves per Vite release;
// a fixed target that is not a browser, `es2020`, passes it and moves the
// bundle, which the committed-bundle diff catches instead.
const VERSIONED_TARGET = /^[a-z]+\d+(\.\d+)*$/;

const statedTarget = async (): Promise<string | string[] | false | undefined> => {
  const loaded = await loadConfigFromFile(
    { command: "build", mode: "production" },
    fileURLToPath(new URL("./vite.config.ts", import.meta.url)),
  );
  return loaded?.config.build?.target;
};

describe("the console's browser floor", () => {
  it("is stated by vite.config.ts rather than left to Vite", async () => {
    const stated = await statedTarget();
    expect(
      stated,
      "vite.config.ts states no build.target, so the floor is Vite's default " +
        "again and the next Vite bump moves it, as a rebuilt bundle that reads " +
        "as re-minification",
    ).toBeTruthy();
  });

  it("names a version for every target in it", async () => {
    const stated = await statedTarget();
    const entries = typeof stated === "string" ? [stated] : stated || [];
    expect(entries.length, "a floor of no targets is not a floor").toBeGreaterThan(0);
    for (const entry of entries) {
      expect(
        entry,
        "a target such as `baseline-widely-available` is Vite's moving default " +
          "written out rather than a floor: it names a different set of browsers " +
          "per Vite version",
      ).toMatch(VERSIONED_TARGET);
    }
  });
});
