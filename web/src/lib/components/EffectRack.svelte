<script lang="ts">
  import FxSlider from "$lib/components/FxSlider.svelte";
  import type { FxLayer } from "$lib/types";

  interface Props {
    effects: FxLayer[];
    readOnly?: boolean;
    onbypass: (layer: number, enabled: boolean) => void;
    onparam: (layer: number, param: string, norm: number) => void;
  }

  let { effects, readOnly = false, onbypass, onparam }: Props = $props();
</script>

{#if effects.length === 0}
  <p class="text-sm text-[var(--ink-dim)]">The current scene has no effect chain.</p>
{:else}
  <div class="space-y-3">
    {#each effects as fx (fx.index)}
      <!-- Rows are generated from the layer's own `LIVE_PARAMS`, which is the
           effect registry itself — so the rack cannot list a knob the layer
           does not have. -->
      <div
        class="rounded-lg border border-[var(--edge)] p-3"
        class:border-dashed={!fx.enabled}
        class:opacity-60={!fx.enabled}
      >
        <div class="mb-2 flex items-center gap-3">
          <span class="truncate text-sm font-medium">{fx.index + 1}. {fx.name}</span>
          <span class="font-mono text-[0.65rem] text-[var(--ink-dim)]">{fx.mod_source}</span>
          <button
            type="button"
            disabled={readOnly}
            onclick={() => onbypass(fx.index, !fx.enabled)}
            class="ms-auto min-h-9 rounded-lg border px-3 font-mono text-xs
                   disabled:cursor-not-allowed disabled:opacity-40
                   focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
                   {fx.enabled
              ? 'border-c64-green/60 text-c64-green'
              : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
          >
            {fx.enabled ? "ON" : "BYPASS"}
          </button>
        </div>
        <div class="space-y-1">
          {#each fx.params as param (param.name)}
            <FxSlider
              {param}
              {readOnly}
              onchange={(norm) => onparam(fx.index, param.name, norm)}
            />
          {/each}
        </div>
      </div>
    {/each}
  </div>
{/if}
