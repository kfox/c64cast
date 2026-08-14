<script lang="ts">
  import type { Clip } from "$lib/types";

  interface Props {
    clips: Clip[];
    readOnly?: boolean;
    onpress: (slot: number, pressed: boolean) => void;
  }

  let { clips, readOnly = false, onpress }: Props = $props();

  // Press and release are sent separately because the launch types need them
  // to be: `gate` holds the clip only while the pad is down and `toggle` acts
  // on the release, while `trigger` ignores it — so press+release is safe for
  // every type and is what the MIDI surface sends too.
  function press(slot: number, pressed: boolean, event: Event): void {
    event.preventDefault();
    if (readOnly) return;
    onpress(slot, pressed);
  }

  // A keyboard-synthesised click has `detail === 0`, which is how it is told
  // apart from the click that follows a real pointer release (already handled).
  function keyFire(slot: number, event: MouseEvent): void {
    if (event.detail !== 0 || readOnly) return;
    onpress(slot, true);
    onpress(slot, false);
  }

  const tone: Record<Clip["state"], string> = {
    active: "border-c64-green text-c64-green bg-c64-green/10",
    armed: "border-c64-yellow text-c64-yellow bg-c64-yellow/10 animate-pulse",
    loaded: "border-[var(--edge)] text-[var(--ink)] bg-[var(--panel-alt)]",
  };
</script>

{#if clips.length === 0}
  <p class="text-sm text-[var(--ink-dim)]">
    No clip grid configured — add <code class="font-mono">[[performance.clips]]</code> to the running
    configuration.
  </p>
{:else}
  <div class="grid grid-cols-[repeat(auto-fill,minmax(7rem,1fr))] gap-2">
    {#each clips as clip (clip.slot)}
      <button
        type="button"
        disabled={readOnly}
        aria-pressed={clip.state === "active"}
        onpointerdown={(e) => press(clip.slot, true, e)}
        onpointerup={(e) => press(clip.slot, false, e)}
        onpointercancel={(e) => press(clip.slot, false, e)}
        onclick={(e) => keyFire(clip.slot, e)}
        class="flex min-h-20 touch-none flex-col justify-between rounded-lg border p-2 text-start
               transition enabled:active:brightness-125 disabled:opacity-50
               focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
               {tone[clip.state]}"
      >
        <span class="line-clamp-2 text-sm font-medium">{clip.name}</span>
        <span class="font-mono text-[0.65rem] text-[var(--ink-dim)]">
          {clip.launch}{clip.loop ? " ⟳" : ""} · {clip.quantize}
        </span>
      </button>
    {/each}
  </div>
{/if}
