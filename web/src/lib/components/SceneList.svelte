<script lang="ts">
  import type { SceneRow } from "$lib/types";

  interface Props {
    scenes: SceneRow[];
    readOnly?: boolean;
    onjump: (index: number) => void;
  }

  let { scenes, readOnly = false, onjump }: Props = $props();

  /** Seconds as the playlist would say them. A scene with no duration runs
   *  until its source ends, which is a fact about the scene rather than a
   *  missing number, so it says so. */
  function length(seconds: number | null): string {
    if (seconds === null) return "until it ends";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  }
</script>

{#if scenes.length === 0}
  <p class="text-sm text-[var(--ink-dim)]">This show has no scenes.</p>
{:else}
  <!-- Bounded and scrolled: a playlist can be a hundred scenes long and this
       sits beside the controls somebody is actually holding. -->
  <ol class="max-h-72 space-y-1 overflow-y-auto">
    {#each scenes as scene (scene.index)}
      <li>
        <button
          type="button"
          disabled={readOnly}
          onclick={() => onjump(scene.index)}
          aria-current={scene.is_current ? "true" : undefined}
          class="flex min-h-11 w-full items-center gap-3 rounded-lg border px-3 text-start
                 disabled:cursor-not-allowed disabled:opacity-40
                 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
                 {scene.is_current
            ? 'border-c64-green/60 bg-[var(--panel-alt)]'
            : 'border-[var(--edge)] hover:border-[var(--ink-dim)]'}"
        >
          <span class="w-6 font-mono text-xs tabular-nums text-[var(--ink-dim)]">
            {scene.index + 1}
          </span>
          <span class="flex-1 truncate text-sm">{scene.name}</span>
          <span class="font-mono text-[0.65rem] text-[var(--ink-dim)]">
            {length(scene.duration_s)}
          </span>
        </button>
      </li>
    {/each}
  </ol>
  <p class="mt-2 text-xs text-[var(--ink-dim)]">
    A jump is a cut — it goes straight to the scene rather than playing the interstitial in front
    of it.
  </p>
{/if}
