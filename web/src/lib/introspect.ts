import { api } from "./api";
import type {
  FieldDoc,
  Introspection,
  MediaEntry,
  MediaIndex,
  OverlayDoc,
  ParamDoc,
  SceneTypeDoc,
  SectionDoc,
  Swatch,
} from "./types";

/** The introspection document, fetched at most once per page load.
 *
 * It is ~150 KB and it describes the *code*: every config field's help,
 * choices, default and `apply`, every scene type's fields already filtered by
 * `applies_to`. None of that can change while the host process is up, so a
 * screen re-mounting must not re-fetch it. The promise itself is the cache —
 * two screens mounting at once share one request — and a failure clears it so
 * a retry is possible. */
let cached: Promise<DocIndex> | null = null;

export function documentation(): Promise<DocIndex> {
  cached ??= api
    .introspect()
    .then((doc) => new DocIndex(doc))
    .catch((e: unknown) => {
      cached = null;
      throw e;
    });
  return cached;
}

/** Introspection turned into the lookups a form actually makes: "what does
 *  `[video].fps` mean", asked once per rendered row. */
export class DocIndex {
  readonly sections: SectionDoc[];
  readonly sceneTypes: SceneTypeDoc[];
  readonly palette: Swatch[];

  readonly #sections = new Map<string, SectionDoc>();
  readonly #sceneTypes = new Map<string, SceneTypeDoc>();
  readonly #fields = new Map<string, FieldDoc>();
  readonly #overlays = new Map<string, OverlayDoc>();
  readonly #params = new Map<string, ParamDoc>();

  constructor(doc: Introspection) {
    this.sections = doc.sections;
    this.sceneTypes = doc.scene_types;
    this.palette = doc.palette ?? [];
    for (const section of doc.sections) {
      this.#sections.set(section.name, section);
      for (const field of section.fields) this.#fields.set(`${section.name}.${field.name}`, field);
    }
    for (const scene of doc.scene_types) {
      this.#sceneTypes.set(scene.name, scene);
      // Scene fields share a namespace with nothing else, and the same field
      // name means different things on different scene types (`source` on
      // `generative` is not `source` on `wled`), so the type is part of the key.
      for (const field of scene.fields) this.#fields.set(`scene:${scene.name}.${field.name}`, field);
    }
    for (const overlay of doc.overlays) {
      this.#overlays.set(overlay.name, overlay);
      for (const param of overlay.params) this.#params.set(`${overlay.name}.${param.name}`, param);
    }
  }

  section(name: string): SectionDoc | undefined {
    return this.#sections.get(name);
  }

  sceneType(name: string): SceneTypeDoc | undefined {
    return this.#sceneTypes.get(name);
  }

  field(section: string, name: string): FieldDoc | undefined {
    return this.#fields.get(`${section}.${name}`);
  }

  sceneField(type: string, name: string): FieldDoc | undefined {
    return this.#fields.get(`scene:${type}.${name}`);
  }

  overlay(name: string): OverlayDoc | undefined {
    return this.#overlays.get(name);
  }

  overlayParam(overlay: string, name: string): ParamDoc | undefined {
    return this.#params.get(`${overlay}.${name}`);
  }
}

/** One `GET /api/media` per kind, cached for the page's lifetime the same
 *  way `documentation()` caches the introspection document — a media kind
 *  describes what's on disk right now rather than the code, but re-listing
 *  it on every scene render would mean one request per field per keystroke.
 *  A failure clears its cache entry so a later attempt can retry. */
const mediaCache = new Map<string, Promise<MediaEntry[]>>();

export function mediaOfKind(kind: string): Promise<MediaEntry[]> {
  let cached = mediaCache.get(kind);
  if (!cached) {
    cached = api
      .media(kind)
      .then((idx) => idx.entries)
      .catch((e: unknown) => {
        mediaCache.delete(kind);
        throw e;
      });
    mediaCache.set(kind, cached);
  }
  return cached;
}

/** Drop a kind's cached listing so the next `mediaOfKind` re-fetches it —
 *  called after an upload, which otherwise would not appear in any datalist
 *  until the page reloaded (this cache is the only reason it wouldn't). */
export function forgetMedia(kind: string): void {
  mediaCache.delete(kind);
}

/** A live query against a media kind, uncached — `mediaOfKind`'s cache is for
 *  the unfiltered listing everyone reads on mount; a query fires on a
 *  debounce and freshness beats a map keyed by every prefix somebody typed.
 *  Returns the whole index, `truncated` included, so a search past
 *  `MAX_FILES` still says so. */
export function searchMedia(kind: string, q: string): Promise<MediaIndex> {
  return api.media(kind, q);
}

export type FieldKind = "bool" | "int" | "float" | "str" | "complex";

/** The wizard's `field_kind()` (c64cast/app/wizard.py), which classifies a
 *  declared type into how it should be presented. Duplicated rather than
 *  served because it is five lines of string matching over data the API
 *  already sends; the ordering is the part worth copying exactly — `float`
 *  before `int` so `"float"` is not read as containing `int`. */
export function fieldKind(type: string): FieldKind {
  const t = type.toLowerCase();
  if (t.includes("list") || t.includes("dict")) return "complex";
  if (t.includes("bool")) return "bool";
  if (t.includes("float")) return "float";
  if (t.includes("int")) return "int";
  return "str";
}

/** Split a declared type at its top-level `|`, leaving the insides of
 *  `list[...]` / `dict[...]` alone — `int | list[int | str]` is two members,
 *  not three. Mirrors `wizard.union_members()`. */
export function unionMembers(type: string): string[] {
  const members: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < type.length; i++) {
    const ch = type[i];
    if (ch === "[" || ch === "(") depth++;
    else if (ch === "]" || ch === ")") depth--;
    else if (ch === "|" && depth === 0) {
      members.push(type.slice(start, i));
      start = i + 1;
    }
  }
  members.push(type.slice(start));
  return members.map((m) => m.trim()).filter((m) => m !== "");
}

/** Every kind a declared type accepts, in declaration order, without repeats
 *  (`wizard.field_kinds()`). `fieldKind` classifies the type as a whole, which
 *  is all a one-question prompt can act on; a form has room for the union, and
 *  losing half of one is how `border` (`int | str`) ends up a number box under
 *  help text that says you may write "light blue". */
export function fieldKinds(type: string): FieldKind[] {
  return [...new Set(unionMembers(type).map(fieldKind))];
}

/** A loaded value as one line of text. Lists and tables get JSON rather than
 *  TOML: they are shown, not edited, and JSON is what the API sent. */
export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "on" : "off";
  if (typeof value === "string") return value === "" ? '""' : value;
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  return String(value);
}
