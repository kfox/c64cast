<script lang="ts">
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
  }

  let {
    name,
    value,
    changed = false,
    help = "",
    type = "",
    choices = [],
    live = false,
  }: Props = $props();

  // Help is a paragraph for some fields and a sentence for others, and the
  // form shows a hundred rows at once with the filter off. Clamped, and
  // expanded by asking — which is also where the choices and the declared type
  // go, so the resting row stays one line.
  let open = $state(false);
  const complex = $derived(fieldKind(type) === "complex");
</script>

<div class="border-l-2 py-1.5 pl-3 {changed ? 'border-[var(--accent)]' : 'border-transparent'}">
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
    {#if !complex}
      <span class="font-mono text-sm text-[var(--ink-dim)]">=</span>
      <span class="font-mono text-sm break-all" class:text-[var(--ink-dim)]={!changed}>
        {showValue(value)}
      </span>
    {/if}
  </div>

  {#if complex}
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
    </dl>
  {/if}
</div>
