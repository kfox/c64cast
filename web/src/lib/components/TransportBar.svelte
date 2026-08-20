<script lang="ts">
  import { formatTime, keyNudgeEvents, loopButtonLabel, slotEnabled } from "$lib/transportBarLogic";
  import type { TransportState } from "$lib/types";

  interface Props {
    transport: TransportState;
    readOnly?: boolean;
    /** One transport verb (`TRANSPORT_VERBS`), plus whatever extra fields it
     *  needs — `perf_console.PerfBridge.transport`'s own parameter names, so
     *  this passes straight through to `{action: "transport", verb, ...extra}`. */
    onverb: (verb: string, extra?: Record<string, unknown>) => void;
  }

  let { transport, readOnly = false, onverb }: Props = $props();

  // The scrub bar holds the dragged position under the finger and only sends
  // on release — the same rule FieldInput and FxSlider follow: a control the
  // state feed echoes back must not have its value yanked mid-gesture.
  let dragging = $state(false);
  let dragValue = $state(0);
  const duration = $derived(transport.duration ?? 0);
  const shownPosition = $derived(dragging ? dragValue : transport.position);

  function onScrubInput(e: Event & { currentTarget: HTMLInputElement }): void {
    dragging = true;
    dragValue = Number(e.currentTarget.value);
  }

  function onScrubCommit(e: Event & { currentTarget: HTMLInputElement }): void {
    const target = Number(e.currentTarget.value);
    dragging = false;
    onverb("seek", { target });
  }

  // Press-and-hold rw/ff: the engine's ramp is itself hold-driven (it
  // accelerates the longer the button stays down), so press and release are
  // sent separately rather than as one tap — the same press/release split
  // ClipGrid uses for launch types that need to tell them apart.
  function hold(action: "rw" | "ff", pressed: boolean, event: Event): void {
    event.preventDefault();
    if (readOnly) return;
    onverb(action, { pressed });
  }

  // A keyboard-synthesized click (Enter/Space on a focused button) has
  // `detail === 0` — there was no pointerdown/up to hold, so it gets a single
  // brief nudge instead of a hang with no release.
  function keyNudge(action: "rw" | "ff", event: MouseEvent): void {
    if (readOnly) return;
    const nudges = keyNudgeEvents(event.detail);
    if (nudges === null) return;
    for (const { pressed } of nudges) onverb(action, { pressed });
  }

  const loop = $derived(transport.loop);
  const loopLabel = $derived(loopButtonLabel(loop.state));

  /** Matches the Looks pad count so the same "SAVE arms, a pad commits"
   *  gesture (see LookPads) applies here for a video's per-file loop presets. */
  const LOOP_SLOTS = 8;
  let savingSlot = $state(false);
  const slots = $derived(Array.from({ length: LOOP_SLOTS }, (_, i) => i + 1));
  const savedSlots = $derived(new Set(transport.loop_slots));
</script>

<div class="space-y-3">
  <div class="flex flex-wrap items-center gap-3">
    <button
      type="button"
      disabled={readOnly}
      aria-pressed={transport.frozen}
      onclick={() => onverb(transport.frozen ? "unfreeze" : "freeze")}
      class="min-h-9 rounded-lg border px-3 font-mono text-xs
             disabled:cursor-not-allowed disabled:opacity-40
             focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
             {transport.frozen
        ? 'border-c64-yellow bg-c64-yellow/15 text-c64-yellow'
        : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
    >
      {transport.frozen ? "Frozen" : "Freeze"}
    </button>
    <span class="font-mono text-xs tabular-nums text-[var(--ink-dim)]">
      {formatTime(shownPosition)}{transport.duration !== null
        ? ` / ${formatTime(transport.duration)}`
        : ""}
    </span>
  </div>

  <input
    type="range"
    min="0"
    max={duration}
    step="0.1"
    disabled={readOnly || duration <= 0}
    value={shownPosition}
    oninput={onScrubInput}
    onchange={onScrubCommit}
    aria-label="Scrub"
    class="w-full accent-[var(--accent)] disabled:opacity-40"
  />

  <div class="flex flex-wrap items-center gap-2">
    <button
      type="button"
      disabled={readOnly}
      onpointerdown={(e) => hold("rw", true, e)}
      onpointerup={(e) => hold("rw", false, e)}
      onpointercancel={(e) => hold("rw", false, e)}
      onclick={(e) => keyNudge("rw", e)}
      class="min-h-9 touch-none rounded-lg border border-[var(--edge)] px-3 font-mono text-xs
             text-[var(--ink)] transition enabled:active:brightness-125 disabled:cursor-not-allowed
             disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2
             focus-visible:outline-[var(--accent)]"
    >
      ◀◀ Rewind
    </button>
    <button
      type="button"
      disabled={readOnly}
      onpointerdown={(e) => hold("ff", true, e)}
      onpointerup={(e) => hold("ff", false, e)}
      onpointercancel={(e) => hold("ff", false, e)}
      onclick={(e) => keyNudge("ff", e)}
      class="min-h-9 touch-none rounded-lg border border-[var(--edge)] px-3 font-mono text-xs
             text-[var(--ink)] transition enabled:active:brightness-125 disabled:cursor-not-allowed
             disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2
             focus-visible:outline-[var(--accent)]"
    >
      Fast-forward ▶▶
    </button>
    <button
      type="button"
      disabled={readOnly}
      aria-pressed={loop.state !== "none"}
      onclick={() => onverb("loop_toggle")}
      class="min-h-9 rounded-lg border px-3 font-mono text-xs
             disabled:cursor-not-allowed disabled:opacity-40
             focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
             {loop.state === 'none'
        ? 'border-[var(--edge)] text-[var(--ink-dim)]'
        : 'border-c64-cyan text-c64-cyan'}"
    >
      {loopLabel}
    </button>
  </div>

  <div class="flex items-center gap-3">
    <button
      type="button"
      disabled={readOnly}
      aria-pressed={savingSlot}
      onclick={() => (savingSlot = !savingSlot)}
      class="min-h-9 rounded-lg border px-3 font-mono text-xs
             disabled:cursor-not-allowed disabled:opacity-40
             focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
             {savingSlot
        ? 'border-c64-yellow bg-c64-yellow/15 text-c64-yellow'
        : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
    >
      SAVE
    </button>
    <p class="text-xs text-[var(--ink-dim)]">
      {savingSlot ? "A pad now stores the current A/B loop." : "A pad recalls its saved loop."}
    </p>
  </div>

  <div class="grid grid-cols-[repeat(auto-fill,minmax(3rem,1fr))] gap-2">
    {#each slots as slot (slot)}
      {@const saved = savedSlots.has(slot)}
      <button
        type="button"
        disabled={readOnly || !slotEnabled(saved, savingSlot)}
        onclick={() => onverb("loop_slot", { slot, save: savingSlot, clear: false })}
        class="aspect-square rounded-lg border font-mono text-sm
               disabled:cursor-not-allowed disabled:opacity-30
               focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
               {saved ? 'border-c64-cyan text-c64-cyan' : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
      >
        {slot}
      </button>
    {/each}
  </div>
</div>
