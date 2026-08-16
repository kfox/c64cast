<script lang="ts">
  import type { FieldKind } from "$lib/introspect";

  interface Props {
    /** Labels the control for a screen reader — the visible name sits in the
     *  row beside it, which is not a `<label>` because a row can hold help,
     *  a badge and a clear button as well. */
    label: string;
    kind: FieldKind;
    /** A non-empty list makes this a picker whatever the declared type says. */
    choices?: string[];
    value: unknown;
    disabled?: boolean;
    /** The parsed value, or `null` and a reason when what is typed is not one
     *  yet. Half a number is not an edit — and it is not a reason to throw
     *  away what was typed either, so the text stays and the save waits. */
    onedit: (value: unknown, error: string) => void;
  }

  let { label, kind, choices = [], value, disabled = false, onedit }: Props = $props();

  // Held only while the field has the caret. The value round-trips through the
  // parent and comes back formatted, so binding straight to it would rewrite
  // "1.50" as "1.5" under the cursor and make the decimal unreachable — the
  // same rule the performance sliders follow while a finger is on them.
  let typing = $state<string | null>(null);

  const text = $derived(typing ?? asText(value));
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
