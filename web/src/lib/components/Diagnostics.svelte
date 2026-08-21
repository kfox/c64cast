<script lang="ts">
  import type { Diagnostic } from "$lib/types";

  interface Props {
    /** From a pre-flight check (`validate_ref`'s collect-all pass) — empty for
     *  a check of unsaved text, which has no file on disk to run it against. */
    diagnostics?: Diagnostic[];
  }

  let { diagnostics = [] }: Props = $props();

  /** `ok` diagnostics are the doctor's "this is fine" notes, not something a
   *  refusal screen needs to repeat back. */
  const shown = $derived(diagnostics.filter((d) => d.level !== "ok"));

  const grouped = $derived.by(() => {
    const byCategory = new Map<string, Diagnostic[]>();
    for (const d of shown) byCategory.set(d.category, [...(byCategory.get(d.category) ?? []), d]);
    return [...byCategory.entries()];
  });
</script>

{#if grouped.length}
  <div class="mt-2 space-y-2">
    {#each grouped as [category, items] (category)}
      <div>
        <p class="text-xs font-semibold tracking-wide text-[var(--ink-dim)] uppercase">
          {category}
        </p>
        <ul class="mt-0.5 list-disc pl-5 text-xs">
          {#each items as d, i (i)}
            <li class={d.level === "error" ? "text-c64-red" : "text-c64-yellow"}>
              <span class="font-mono">{d.subject}</span>: {d.message}
              {#if d.hint}
                <span class="text-[var(--ink-dim)]"> — {d.hint}</span>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </div>
{/if}
