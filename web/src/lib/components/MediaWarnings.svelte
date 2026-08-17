<script lang="ts">
  import type { Warning } from "$lib/types";

  interface Props {
    warnings?: Warning[];
    /** Shown when a report is otherwise a clean pass, so "validates" doesn't
     *  read as "will run". Off where the report already failed — a second
     *  heading under an error is noise. */
    heading?: string;
  }

  let { warnings = [], heading = "This will load, but:" }: Props = $props();
</script>

{#if warnings.length}
  <div class="mt-1 text-c64-yellow">
    <p class="text-xs">{heading}</p>
    <ul class="mt-0.5 list-disc pl-5 text-xs">
      {#each warnings as warning, i (i)}
        <li>
          {#if warning.system}<span class="font-mono">{warning.system}</span>:{/if}
          {warning.detail}
        </li>
      {/each}
    </ul>
  </div>
{/if}
