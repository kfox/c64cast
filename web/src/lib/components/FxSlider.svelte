<script lang="ts">
  import type { Knob } from "$lib/types";

  interface Props {
    param: Knob;
    readOnly?: boolean;
    onchange: (norm: number) => void;
  }

  let { param, readOnly = false, onchange }: Props = $props();

  /** The slider's own position while the performer is on it. The host echoes
   *  the value back about three times a second, and letting that echo drive
   *  the input mid-gesture drags the handle backwards under the finger — so
   *  while the control has focus it shows what the performer set, not what the
   *  last frame said. */
  let held = $state(false);
  let local = $state(0);

  const STEPS = 1000;

  const position = $derived(held ? local : Math.round(param.norm * STEPS));
  const shown = $derived(param.min + (position / STEPS) * (param.max - param.min));

  function grab(): void {
    local = Math.round(param.norm * STEPS);
    held = true;
  }

  function move(event: Event): void {
    const el = event.currentTarget as HTMLInputElement;
    local = Number(el.value);
    held = true;
    onchange(local / STEPS);
  }

  // A pointer drag on a range input does not always leave it focused (Safari
  // on iOS does not), so the hold is released when the gesture ends unless the
  // keyboard is what is driving it.
  function settle(event: Event): void {
    held = (event.currentTarget as HTMLInputElement).matches(":focus");
  }
</script>

<div class="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)_auto] items-center gap-3">
  <!-- `aria-label` rather than a `<label for>`: a param name is unique within
       its layer but not across the rack, and two layers with an `amount` would
       otherwise share an id. -->
  <span class="truncate font-mono text-xs text-[var(--ink-dim)]">{param.name}</span>
  <input
    aria-label={param.name}
    type="range"
    min="0"
    max={STEPS}
    step="1"
    value={position}
    disabled={readOnly}
    onfocus={grab}
    oninput={move}
    onchange={settle}
    onblur={() => (held = false)}
    class="h-11 w-full accent-[var(--accent)] disabled:opacity-40"
  />
  <span class="w-12 text-end font-mono text-xs tabular-nums">{shown.toFixed(2)}</span>
</div>
