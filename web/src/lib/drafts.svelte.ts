import type { ConfigEdit } from "./types";

/**
 * Unsaved config edits, held for the whole app rather than by the screen that
 * takes them: the Config screen is unmounted the moment somebody looks at
 * Live, and the tab bar has to say there is something unsaved somewhere.
 *
 * Two editors, one store, keyed by config ref: `text` is the raw editor's
 * draft, `fields` the generated form's staged edits. In memory only, so a
 * reload discards them.
 */
class Drafts {
  /** Raw editor text, per ref, only while it differs from what is on disk. */
  #text = $state<Record<string, string>>({});
  /** Form edits, per ref, keyed by row. */
  #fields = $state<Record<string, Record<string, ConfigEdit>>>({});

  text(ref: string): string | undefined {
    return this.#text[ref];
  }

  fields(ref: string): Record<string, ConfigEdit> {
    return this.#fields[ref] ?? {};
  }

  /** Pass null once the text matches the file again: an edit that restores
   *  the original is not one. */
  setText(ref: string, text: string | null): void {
    const next = { ...this.#text };
    if (text === null) delete next[ref];
    else next[ref] = text;
    this.#text = next;
  }

  setFields(ref: string, edits: Record<string, ConfigEdit>): void {
    const next = { ...this.#fields };
    if (Object.keys(edits).length === 0) delete next[ref];
    else next[ref] = edits;
    this.#fields = next;
  }

  /** Forget everything about one ref — what a successful save does. */
  clear(ref: string): void {
    this.setText(ref, null);
    this.setFields(ref, {});
  }

  /** Every ref with something unsaved, for the file list's markers. */
  get refs(): string[] {
    return [...new Set([...Object.keys(this.#text), ...Object.keys(this.#fields)])];
  }

  get count(): number {
    return this.refs.length;
  }
}

/** One store for the app — a module-level instance, since there is exactly
 *  one console per page. */
export const drafts = new Drafts();
