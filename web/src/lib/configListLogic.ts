/** Pure logic behind `ConfigList.svelte`'s search box, sort control and name
 *  display — pulled out of the component so it can be unit-tested without
 *  mounting Svelte. */

import type { ConfigFile } from "./types";

export type ConfigSort = "name-asc" | "name-desc" | "newest" | "oldest";

/** The display label: `rel` (the ref with its root label already stripped by
 *  the server) with the `.toml` suffix stripped too. Subdirectories stay. */
export function displayLabel(file: ConfigFile): string {
  return file.rel.replace(/\.toml$/i, "");
}

const COMPARATORS: Record<ConfigSort, (a: ConfigFile, b: ConfigFile) => number> = {
  "name-asc": (a, b) => displayLabel(a).localeCompare(displayLabel(b)),
  "name-desc": (a, b) => displayLabel(b).localeCompare(displayLabel(a)),
  newest: (a, b) => b.mtime - a.mtime,
  oldest: (a, b) => a.mtime - b.mtime,
};

export interface VisibleRowsOptions {
  query?: string;
  showExamples?: boolean;
  sort?: ConfigSort;
}

/** The rows `ConfigList` renders: examples dropped unless asked for, filtered
 *  by a case-insensitive substring match against the display label, then
 *  sorted. Never mutates `files`. */
export function visibleRows(
  files: readonly ConfigFile[],
  { query = "", showExamples = false, sort = "name-asc" }: VisibleRowsOptions = {},
): ConfigFile[] {
  const needle = query.trim().toLowerCase();
  return files
    .filter((f) => showExamples || !f.readonly)
    .filter((f) => !needle || displayLabel(f).toLowerCase().includes(needle))
    .slice()
    .sort(COMPARATORS[sort]);
}
