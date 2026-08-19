<script lang="ts">
  import type { Swatch } from "$lib/types";

  interface Props {
    /** Labels the group for a screen reader — the visible name is in the row. */
    label: string;
    palette: Swatch[];
    /** Whitelist rather than one color: every swatch toggles, and the value is
     *  the list. */
    multi?: boolean;
    value: unknown;
    disabled?: boolean;
    /** A color name, or the list of them. Names rather than indices because a
     *  name is the half of `int | str` the form could not reach before, and it
     *  is the half that still reads as a color a year later. */
    onpick: (value: unknown) => void;
  }

  let { label, palette, multi = false, value, disabled = false, onpick }: Props = $props();

  /** Which swatch a written value names, or -1. Indices and the two canonical
   *  spellings are matched here; the fuzzy forms the loader also accepts
   *  ("lgrn", "blk") are resolved on the host, so the picker says it does not
   *  recognize them rather than quietly selecting the wrong one. */
  function indexOf(v: unknown): number {
    if (typeof v === "number") return Number.isInteger(v) && v >= 0 && v < palette.length ? v : -1;
    if (typeof v !== "string") return -1;
    const want = v.trim().toLowerCase();
    return palette.findIndex((s) => s.name === want || s.label.toLowerCase() === want);
  }

  const chosen = $derived(multi ? [] : [indexOf(value)].filter((i) => i >= 0));
  const list = $derived(Array.isArray(value) ? value : []);
  const picked = $derived(multi ? list.map(indexOf).filter((i) => i >= 0) : chosen);
  const unknown = $derived(
    multi ? list.filter((v) => indexOf(v) < 0) : indexOf(value) < 0 && value !== null ? [value] : [],
  );

  function pick(swatch: Swatch): void {
    if (!multi) {
      onpick(swatch.name);
      return;
    }
    const on = new Set(picked);
    if (on.has(swatch.index)) on.delete(swatch.index);
    else on.add(swatch.index);
    // Palette order, not click order: the value is a set of allowed colors,
    // and a stable order keeps the saved file from churning on a re-pick.
    onpick(palette.filter((s) => on.has(s.index)).map((s) => s.name));
  }
</script>

<div
  role={multi ? "group" : "radiogroup"}
  aria-label={label}
  class="flex flex-wrap gap-1"
  aria-disabled={disabled || undefined}
>
  {#each palette as swatch (swatch.index)}
    {@const on = picked.includes(swatch.index)}
    <button
      type="button"
      {disabled}
      role={multi ? "switch" : "radio"}
      aria-checked={on}
      title="{swatch.label} ({swatch.index})"
      onclick={() => pick(swatch)}
      class="size-8 rounded-md border-2 disabled:opacity-40
             focus-visible:outline-2 focus-visible:outline-offset-1
             focus-visible:outline-[var(--accent)]
             {on ? 'border-[var(--accent)]' : 'border-[var(--edge)]'}"
      style="background: {swatch.hex}"
    >
      <span class="sr-only">{swatch.label}</span>
    </button>
  {/each}
</div>

<p class="mt-1 text-xs text-[var(--ink-dim)]">
  {#if unknown.length}
    <!-- Never silently re-selected: the loader takes spellings this picker
         cannot place, and dropping one would edit the file by being looked at. -->
    <span class="text-c64-yellow">
      {unknown.map((v) => JSON.stringify(v)).join(", ")}
      {unknown.length === 1 ? "is not a name this picker knows" : "are not names this picker knows"}
      — the host may still accept {unknown.length === 1 ? "it" : "them"}. Picking here replaces
      {unknown.length === 1 ? "it" : "them"}.
    </span>
  {:else if multi}
    {picked.length === 0
      ? "No colors chosen yet."
      : `${picked.length} color${picked.length === 1 ? "" : "s"}: ${picked.map((i) => palette[i].label).join(", ")}`}
  {:else if picked.length}
    {palette[picked[0]].label}
  {/if}
</p>
