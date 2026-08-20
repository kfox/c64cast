<script lang="ts">
  interface Props {
    /** Slots that hold a saved look. */
    looks: number[];
    readOnly?: boolean;
    onlook: (slot: number, save: boolean) => void;
  }

  let { looks, readOnly = false, onlook }: Props = $props();

  /** Matches the `/perf` console's pad count so a look saved from one surface
   *  is reachable from the other. */
  const SLOTS = 8;

  // A save arms first and fires on the pad, rather than each pad carrying two
  // buttons: on a phone at a gig the pads have to stay big, and recall is the
  // move that has to be fast. An *empty* pad has nothing to lose, though, so
  // it saves on a plain tap without arming SAVE first — only a pad that would
  // overwrite an existing look needs the arm-then-tap safety.
  let saving = $state(false);

  const saved = $derived(new Set(looks));
  const slots = $derived(Array.from({ length: SLOTS }, (_, i) => i + 1));

  function tap(slot: number, isSaved: boolean): void {
    onlook(slot, saving || !isSaved);
  }
</script>

<div class="mb-3 flex items-center gap-3">
  <button
    type="button"
    disabled={readOnly}
    aria-pressed={saving}
    onclick={() => (saving = !saving)}
    class="min-h-9 rounded-lg border px-3 font-mono text-xs
           disabled:cursor-not-allowed disabled:opacity-40
           focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
           {saving
      ? 'border-c64-yellow bg-c64-yellow/15 text-c64-yellow'
      : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
  >
    SAVE
  </button>
  <p class="text-xs text-[var(--ink-dim)]">
    {saving
      ? "A pad now stores the current clip and effect chain."
      : "A pad recalls its look — an empty pad saves one instead."}
  </p>
</div>

<div class="grid grid-cols-[repeat(auto-fill,minmax(3.5rem,1fr))] gap-2">
  {#each slots as slot (slot)}
    {@const isSaved = saved.has(slot)}
    <button
      type="button"
      disabled={readOnly}
      onclick={() => tap(slot, isSaved)}
      class="aspect-square rounded-lg border font-mono text-sm
             disabled:cursor-not-allowed disabled:opacity-40
             focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
             {isSaved
        ? 'border-c64-cyan text-c64-cyan'
        : 'border-dashed border-[var(--edge)] text-[var(--ink-dim)]'}"
    >
      {isSaved ? slot : "+"}
    </button>
  {/each}
</div>
