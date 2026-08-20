/** Pure logic behind the Editor's media picker — the `file =` field's
 *  datalist options, which scene type browses which media kind, and reading a
 *  URL out of a drag-and-drop payload — pulled out of the components so it
 *  can be unit-tested without mounting Svelte or a `DataTransfer`. */

import type { MediaEntry, MediaUploaded, SceneTypeDoc } from "./types";

/** The datalist options for a `file =` field: directories first (the more
 *  useful default to reach for — a slideshow or an HVSC tree is usually
 *  offered as a directory, not one file at a time), each group alphabetical.
 *  Never mutates `entries`. */
export function pickerOptions(entries: readonly MediaEntry[]): string[] {
  const bySpec = (a: MediaEntry, b: MediaEntry) => a.spec.localeCompare(b.spec);
  const dirs = entries.filter((e) => e.is_dir).slice().sort(bySpec);
  const files = entries.filter((e) => !e.is_dir).slice().sort(bySpec);
  return [...dirs, ...files].map((e) => e.spec);
}

/** The media kind(s) a scene type's `file =` field browses, or `[]` for a
 *  type with no `file =` at all (or one `docs.ts` hasn't loaded yet). */
export function kindsForScene(doc: SceneTypeDoc | undefined): string[] {
  return doc?.media_kinds ?? [];
}

const URL_LINE = /^https?:\/\/\S+$/i;

/** The first `http(s)://` line out of a drop's `text/uri-list` /
 *  `text/plain` payload, or `null` — a `file:///` path or a bare filename is a
 *  dropped file, not a URL, and goes through `api.uploadMedia` instead (the
 *  component checks `dataTransfer.files` before calling this, since a Finder
 *  or Explorer drag carries both).
 *
 *  Takes a plain string map rather than a `DataTransfer` so this stays
 *  testable under `vitest.config.ts`'s `environment: "node"`; the component
 *  reads `event.dataTransfer.getData(...)` into one before calling this. */
export function urlFromDrop(payload: Readonly<Record<string, string>>): string | null {
  const text = payload["text/uri-list"] || payload["text/plain"] || "";
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith("#") && URL_LINE.test(trimmed)) return trimmed;
  }
  return null;
}

/** The one-sentence success banner for a finished upload — the only new logic
 *  worth its own pure function; everything else about the upload flow is
 *  fetch + component state. Says when the name was changed to avoid clobbering
 *  something already there, since a silent rename is the kind of thing an
 *  operator notices five minutes later instead of right away. */
export function uploadMessage(uploaded: MediaUploaded): string {
  if (uploaded.renamed) {
    return `Uploaded as ${uploaded.name} — that name was already taken.`;
  }
  return `Uploaded ${uploaded.name}.`;
}
