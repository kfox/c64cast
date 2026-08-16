<script lang="ts">
  import FieldInput from "$lib/components/FieldInput.svelte";
  import { fieldKind, showValue } from "$lib/introspect";

  interface Props {
    name: string;
    value: unknown;
    /** Left-marked when the file says something other than the default. The
     *  same flag the "only what I've changed" filter runs on. */
    changed?: boolean;
    help?: string;
    /** The declared type, which decides how the value is rendered — a list or
     *  a table gets a block of its own rather than being crushed onto a line. */
    type?: string;
    choices?: string[];
    /** True for a field that takes effect without a restart. Not shown for the
     *  others: every top-level config field needs a rebuild, so a badge on
     *  each of them would be 167 badges saying nothing. */
    live?: boolean;
    /** A widget instead of a printed value. Off for the rows nothing can edit
     *  — an overlay's parameters, and every row of a read-only console. */
    editable?: boolean;
    /** Why this row prints rather than edits, when the reason is about the
     *  field and not about the token. Unset rows just print. */
    locked?: string;
    /** What the field falls back to when the file stops naming it, which is
     *  what `Clear` will leave behind. */
    baseline?: unknown;
    /** Set while this row carries an edit that hasn't been saved. */
    dirty?: boolean;
    /** Why what is typed isn't a value yet. Blocks the save, and says so here
     *  rather than in a summary the row would have to be found from. */
    error?: string;
    onedit?: (value: unknown, error: string) => void;
    /** Put the field back to `baseline` — how a form removes a key, as opposed
     *  to setting one to the same text. */
    onclear?: () => void;
    /** Drop the unsaved edit and show what is on disk again. */
    onrevert?: () => void;
  }

  let {
    name,
    value,
    changed = false,
    help = "",
    type = "",
    choices = [],
    live = false,
    editable = false,
    locked = "",
    baseline = undefined,
    dirty = false,
    error = "",
    onedit,
    onclear,
    onrevert,
  }: Props = $props();

  // Help is a paragraph for some fields and a sentence for others, and the
  // form shows a hundred rows at once with the filter off. Clamped, and
  // expanded by asking — which is also where the choices and the declared type
  // go, so the resting row stays one line.
  let open = $state(false);
  const kind = $derived(fieldKind(type));
  const complex = $derived(kind === "complex");
  // Nothing to clear when the file already says nothing about the field.
  const clearable = $derived(changed || dirty);

  const chip = `min-h-9 rounded-md border border-[var(--edge)] px-2 text-xs
                text-[var(--ink-dim)] hover:text-[var(--ink)]`;
</script>

<div
  class="border-l-2 py-1.5 pl-3
         {dirty ? 'border-c64-yellow' : changed ? 'border-[var(--accent)]' : 'border-transparent'}"
>
  <div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
    <span class="font-mono text-sm">{name}</span>
    {#if live}
      <span
        class="rounded-full border border-c64-green/50 px-1.5 text-[0.65rem] tracking-wide
               text-c64-green uppercase"
        title="Takes effect without restarting the session"
      >
        live
      </span>
    {/if}
    {#if dirty}
      <span class="text-[0.65rem] tracking-wide text-c64-yellow uppercase">edited</span>
    {/if}
    {#if locked}
      <span
        class="rounded-full border border-[var(--edge)] px-1.5 text-[0.65rem] tracking-wide
               text-[var(--ink-dim)] uppercase"
        title={locked}
      >
        text only
      </span>
    {/if}
    {#if !editable && !complex}
      <span class="font-mono text-sm text-[var(--ink-dim)]">=</span>
      <span class="font-mono text-sm break-all" class:text-[var(--ink-dim)]={!changed}>
        {showValue(value)}
      </span>
    {/if}
  </div>

  {#if editable}
    <div class="mt-1 flex flex-wrap items-start gap-2">
      <div class="min-w-40 flex-1">
        <FieldInput
          label={name}
          {kind}
          {choices}
          {value}
          onedit={(v, e) => onedit?.(v, e)}
        />
      </div>
      {#if dirty}
        <button class={chip} onclick={() => onrevert?.()} title="Drop this unsaved edit">
          Undo
        </button>
      {/if}
      {#if clearable}
        <button
          class={chip}
          onclick={() => onclear?.()}
          title="Stop setting this here — {showValue(baseline)} applies"
        >
          Clear
        </button>
      {/if}
    </div>
    {#if error}
      <p class="mt-1 text-xs text-c64-red">{error}</p>
    {/if}
  {:else if complex}
    <pre
      class="mt-1 max-h-48 overflow-auto rounded-md bg-[var(--panel-alt)] p-2
             font-mono text-xs whitespace-pre">{JSON.stringify(value, null, 1)}</pre>
  {/if}

  {#if help || choices.length || type}
    <button
      onclick={() => (open = !open)}
      aria-expanded={open}
      class="mt-0.5 block w-full text-left text-xs text-[var(--ink-dim)] hover:text-[var(--ink)]"
    >
      <span class={open ? "" : "line-clamp-1"}>{help || type}</span>
    </button>
  {/if}

  {#if open}
    <dl class="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 text-xs text-[var(--ink-dim)]">
      <dt>type</dt>
      <dd class="font-mono">{type || "—"}</dd>
      {#if choices.length}
        <dt>choices</dt>
        <dd class="font-mono break-all">{choices.join(" · ")}</dd>
      {/if}
      {#if baseline !== undefined}
        <dt>unset</dt>
        <dd class="font-mono break-all">{showValue(baseline)}</dd>
      {/if}
    </dl>
  {/if}
</div>
