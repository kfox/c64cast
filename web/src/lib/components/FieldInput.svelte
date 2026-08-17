<script lang="ts">
  import ColorSwatches from "$lib/components/ColorSwatches.svelte";
  import type { FieldKind } from "$lib/introspect";
  import type { Swatch } from "$lib/types";

  interface Props {
    /** Labels the control for a screen reader — the visible name sits in the
     *  row beside it, which is not a `<label>` because a row can hold help,
     *  a badge and a clear button as well. */
    label: string;
    /** Every kind the declared type accepts. More than one gets a selector, so
     *  a `int | str` field offers both halves instead of the one the
     *  classifier happened to name first. */
    kinds: FieldKind[];
    /** A non-empty list makes this a picker whatever the declared type says. */
    choices?: string[];
    /** The named set this field's strings come from (`FieldDoc.vocabulary`).
     *  `"c64color"` swaps the text box for the palette. */
    vocabulary?: string;
    palette?: Swatch[];
    value: unknown;
    disabled?: boolean;
    /** The parsed value, or `null` and a reason when what is typed is not one
     *  yet. Half a number is not an edit — and it is not a reason to throw
     *  away what was typed either, so the text stays and the save waits. */
    onedit: (value: unknown, error: string) => void;
  }

  let {
    label,
    kinds,
    choices = [],
    vocabulary = "",
    palette = [],
    value,
    disabled = false,
    onedit,
  }: Props = $props();

  /** Which half of a union the value in hand already is, so a field opens on
   *  the control that can show it rather than on whichever member was declared
   *  first. */
  function kindOf(v: unknown): FieldKind | null {
    if (typeof v === "boolean") return "bool";
    if (typeof v === "object" && v !== null) return "complex";
    if (typeof v === "number") return Number.isInteger(v) ? "int" : "float";
    if (typeof v === "string") return "str";
    return null;
  }

  // Sticky once touched: switching to the number box and typing nothing yet
  // must not bounce back to the text box on the next render.
  let chosen = $state<FieldKind | null>(null);
  const fromValue = $derived(kinds.find((k) => k === kindOf(value)) ?? null);
  const kind = $derived(chosen ?? fromValue ?? kinds[0] ?? "str");
  const swatches = $derived(vocabulary === "c64color" && palette.length > 0);

  const KIND_LABELS: Record<FieldKind, string> = {
    bool: "on/off",
    int: "number",
    float: "number",
    str: "text",
    complex: "list",
  };

  const kindLabel = (k: FieldKind) =>
    swatches
      ? k === "str"
        ? "colour"
        : k === "complex"
          ? "colours"
          : KIND_LABELS[k]
      : KIND_LABELS[k];

  /** Switching halves shows an empty control rather than the stored value:
   *  what is stored is the *other* type, and rendering it here would put
   *  "light blue" in a number box. Nothing is staged until something is
   *  entered, so switching back is free. */
  function switchTo(k: FieldKind): void {
    chosen = k;
    typing = null;
  }

  // The value only belongs to the control currently on screen when it is
  // already of that kind; otherwise the control starts empty.
  const shown = $derived(fromValue === kind ? value : null);

  // Held only while the field has the caret. The value round-trips through the
  // parent and comes back formatted, so binding straight to it would rewrite
  // "1.50" as "1.5" under the cursor and make the decimal unreachable — the
  // same rule the performance sliders follow while a finger is on them.
  let typing = $state<string | null>(null);

  const text = $derived(typing ?? asText(shown));
  const picker = $derived(choices.length > 0);

  function asText(v: unknown): string {
    if (v === null || v === undefined) return "";
    if (typeof v === "object") return JSON.stringify(v, null, 1);
    return String(v);
  }

  /** What the typed text means, or why it doesn't mean anything yet. */
  function parse(raw: string): [unknown, string] {
    if (kind === "str") return [raw, ""];
    if (kind === "complex") {
      if (!raw.trim()) return [null, "needs a list or a table, as JSON"];
      try {
        return [JSON.parse(raw), ""];
      } catch {
        return [null, "not valid JSON yet"];
      }
    }
    if (!raw.trim()) return [null, "needs a number"];
    const n = Number(raw);
    if (!Number.isFinite(n)) return [null, "not a number"];
    if (kind === "int" && !Number.isInteger(n)) return [null, "needs a whole number"];
    return [n, ""];
  }

  function type(raw: string): void {
    typing = raw;
    const [parsed, error] = parse(raw);
    onedit(parsed, error);
  }

  // Releasing the caret hands the field back to the value, so what is shown is
  // what would be saved rather than what happened to be typed.
  const settle = () => (typing = null);

  const box = `min-h-11 w-full rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
               px-2 py-1 font-mono text-sm disabled:opacity-40
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]`;
</script>

<!-- A union offers its halves before the control, because which half you are
     writing changes what the control below even is. One kind renders nothing
     here, which is every field but a handful. -->
{#if kinds.length > 1 && !picker}
  <div class="mb-1 flex gap-1" role="group" aria-label="{label}: how to write it">
    {#each kinds as k (k)}
      <button
        type="button"
        {disabled}
        aria-pressed={kind === k}
        onclick={() => switchTo(k)}
        class="min-h-9 rounded-md border px-2 text-xs disabled:opacity-40
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]
               {kind === k
          ? 'border-[var(--accent)] text-[var(--ink)]'
          : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
      >
        {kindLabel(k)}
      </button>
    {/each}
  </div>
{/if}

{#if kind === "bool" && !picker}
  <label class="flex min-h-11 items-center gap-2 text-sm">
    <input
      type="checkbox"
      class="size-5"
      checked={value === true}
      {disabled}
      aria-label={label}
      onchange={(e) => onedit(e.currentTarget.checked, "")}
    />
    <span class="text-[var(--ink-dim)]">{value === true ? "on" : "off"}</span>
  </label>
{:else if !picker && swatches && (kind === "str" || kind === "complex")}
  <ColorSwatches
    {label}
    {palette}
    multi={kind === "complex"}
    value={shown}
    {disabled}
    onpick={(v) => onedit(v, "")}
  />
{:else if picker}
  <select
    class={box}
    {disabled}
    aria-label={label}
    value={asText(value)}
    onchange={(e) => onedit(e.currentTarget.value, "")}
  >
    <!-- A value the choices don't cover is still the value: showing it as an
         option is how the picker avoids silently re-selecting something else. -->
    {#if !choices.includes(asText(value))}
      <option value={asText(value)}>{asText(value) || "—"}</option>
    {/if}
    {#each choices as choice (choice)}
      <option value={choice}>{choice}</option>
    {/each}
  </select>
{:else if kind === "complex"}
  <textarea
    class="{box} h-24 resize-y whitespace-pre"
    spellcheck="false"
    autocapitalize="off"
    {disabled}
    aria-label={label}
    value={text}
    oninput={(e) => type(e.currentTarget.value)}
    onblur={settle}
  ></textarea>
{:else}
  <input
    class={box}
    type={kind === "str" ? "text" : "number"}
    inputmode={kind === "int" ? "numeric" : kind === "float" ? "decimal" : undefined}
    step={kind === "float" ? "any" : kind === "int" ? "1" : undefined}
    spellcheck="false"
    autocapitalize="off"
    {disabled}
    aria-label={label}
    value={text}
    oninput={(e) => type(e.currentTarget.value)}
    onblur={settle}
  />
{/if}
