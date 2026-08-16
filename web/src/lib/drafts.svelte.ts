import type { ConfigEdit } from "./types";

/**
 * Unsaved config edits, held for the whole app rather than by the screen that
 * takes them.
 *
 * Two reasons it is not component state. The Config screen is unmounted the
 * moment somebody looks at Live — so edits typed and then checked against the
 * running show used to be gone on the way back, which is the worst possible
 * time to lose them. And the tab bar has to be able to say that there is
 * something unsaved *somewhere*, which it cannot ask a screen that no longer
 * exists.
 *
 * Two editors, one store, keyed by config ref: `text` is the raw editor's
 * draft, `fields` the generated form's staged edits. Deliberately in memory
 * only — a draft that outlives the page would be a second copy of the config
 * with no way to see it — so a reload still discards them, as it always did.
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

  /** Pass null once the text matches the file again — an "edit" that restores
   *  the original is not one, and leaving it marked would keep the ref lit. */
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

  /** Forget everything about one ref. What a successful save does: the file
   *  now says it, so it is no longer an edit. */
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

/** One store for the app. A module-level instance rather than context: there is
 *  exactly one console per page, and a second one would be a second set of
 *  unsaved edits nobody could reach. */
export const drafts = new Drafts();
