<script lang="ts">
  import { showValue } from "$lib/introspect";
  import type { LayerNote } from "$lib/types";

  interface Props {
    layers: LayerNote[];
  }

  let { layers = [] }: Props = $props();
</script>

{#if layers.length}
  <!-- A config is validated with the machine-settings layer underneath it, so a
       stray value there refuses every config on the host — with an error naming
       a section that is nowhere in the file on screen. This is the only place
       the reader can be told to go and look somewhere else. -->
  <div class="mt-2 rounded-md bg-[var(--panel-alt)] px-3 py-2 text-[var(--ink)]">
    <p class="text-xs">This file may not be the problem:</p>
    <ul class="mt-1 space-y-0.5 font-mono text-xs">
      {#each layers as note, i (i)}
        <li class="break-all">
          {#if note.error}
            {note.path} — {note.error}
          {:else}
            [{note.section}] {note.key} = {showValue(note.value)}
            <span class="text-[var(--ink-dim)]">from {note.path}</span>
          {/if}
        </li>
      {/each}
    </ul>
    <p class="mt-1 text-xs text-[var(--ink-dim)]">
      Machine settings apply to every run on this host and this file does not set these, so the
      value shown above is what the loader used.
    </p>
  </div>
{/if}
