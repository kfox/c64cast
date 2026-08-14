<script lang="ts">
  import Button from "$lib/components/Button.svelte";
  import type { ArmedClip, TempoState } from "$lib/types";

  interface Props {
    tempo: TempoState;
    scene: string | null;
    armed: ArmedClip | null;
    readOnly?: boolean;
    ontap: () => void;
  }

  let { tempo, scene, armed, readOnly = false, ontap }: Props = $props();

  /** Where the beat clock was when the host last told us, and when we heard it.
   *  Deliberately a plain object rather than `$state`: the frame loop below
   *  reads it every frame, and a reactive read there would re-arm the effect
   *  that owns the loop on every push. */
  let anchor = { bpm: 0, running: false, phase: 0, bpb: 4, at: 0 };

  let beat = $state(0);
  let lit = $state(false);

  $effect(() => {
    anchor = {
      bpm: tempo.bpm,
      running: tempo.running,
      phase: tempo.beat_phase,
      bpb: tempo.beats_per_bar || 4,
      at: performance.now(),
    };
  });

  // The pulse is extrapolated locally between pushes. The host sends about
  // three frames a second and a beat at 128 bpm is shorter than that, so a
  // pulse driven by the feed alone would stutter and skip beats outright.
  $effect(() => {
    let frame = 0;
    const tick = (): void => {
      const a = anchor;
      let phase = a.phase;
      if (a.running) phase += ((performance.now() - a.at) / 1000) * (a.bpm / 60);
      const whole = Math.floor(phase);
      beat = ((whole % a.bpb) + a.bpb) % a.bpb;
      lit = a.running && phase - whole < 0.5;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  });

  const dots = $derived(Array.from({ length: tempo.beats_per_bar || 4 }, (_, i) => i));
  const countIn = $derived(
    armed === null
      ? ""
      : armed.beats_remaining === null
        ? `arming slot ${armed.slot}`
        : `arming slot ${armed.slot} in ${Math.max(0, Math.ceil(armed.beats_remaining))}`,
  );
</script>

<div class="flex flex-wrap items-center gap-x-5 gap-y-3">
  <p class="font-mono text-3xl leading-none tabular-nums">
    {tempo.bpm ? tempo.bpm.toFixed(0) : "--"}
    <span class="text-sm text-[var(--ink-dim)]">bpm</span>
  </p>

  <div class="flex gap-1.5" role="presentation">
    {#each dots as i (i)}
      <span
        class="size-3 rounded-full border transition-colors
               {i === 0 ? 'border-[var(--ink-dim)]' : 'border-[var(--edge)]'}"
        class:bg-c64-green={lit && i === beat}
        class:border-c64-green={lit && i === beat}
      ></span>
    {/each}
  </div>

  <p class="font-mono text-xs text-[var(--ink-dim)]">
    {tempo.source}{tempo.running ? "" : " · stopped"}
  </p>

  {#if countIn}
    <p class="font-mono text-xs text-c64-yellow">· {countIn}</p>
  {/if}

  <div class="ms-auto flex items-center gap-3">
    {#if scene}
      <p class="truncate font-mono text-xs text-[var(--ink-dim)]">scene: {scene}</p>
    {/if}
    <Button disabled={readOnly} onclick={ontap}>Tap</Button>
  </div>
</div>
