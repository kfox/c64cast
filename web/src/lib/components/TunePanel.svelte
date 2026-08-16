<script lang="ts">
  import FxSlider from "$lib/components/FxSlider.svelte";
  import type { Knob, LiveKnob } from "$lib/types";

  interface Props {
    knobs: LiveKnob[];
    readOnly?: boolean;
    onscalar: (target: string, norm: number) => void;
    onchoice: (target: string, value: string) => void;
  }

  let { knobs, readOnly = false, onscalar, onchoice }: Props = $props();

  /** Grouped the way the host grouped them — `Color pipeline`, `Generator`,
   *  `Scope` — which is `introspect.live_targets()`'s own grouping and so the
   *  same one the `--midi-setup` picker offers. Insertion order is kept rather
   *  than sorted: it is the order the pipeline runs in. */
  const groups = $derived.by(() => {
    const out: { name: string; knobs: LiveKnob[] }[] = [];
    for (const knob of knobs) {
      const group = out.find((g) => g.name === knob.group);
      if (group) group.knobs.push(knob);
      else out.push({ name: knob.group, knobs: [knob] });
    }
    return out;
  });

  /** A scalar knob as the shape the shared slider reads. The host sends value,
   *  range and position together, so nothing has to be recomputed here. */
  function slider(knob: LiveKnob): Knob {
    return {
      name: knob.name,
      value: typeof knob.value === "number" ? knob.value : 0,
      min: knob.min ?? 0,
      max: knob.max ?? 1,
      norm: knob.norm ?? 0,
    };
  }
</script>

{#if knobs.length === 0}
  <p class="text-sm text-[var(--ink-dim)]">
    The current scene has nothing live-tunable — a blank scene has no generator, and a PETSCII
    scene has no dither.
  </p>
{:else}
  <div class="space-y-4">
    {#each groups as group (group.name)}
      <section>
        <h3 class="mb-1 text-xs font-semibold tracking-wide text-[var(--ink-dim)] uppercase">
          {group.name}
        </h3>
        <div class="space-y-1">
          {#each group.knobs as knob (knob.target)}
            {#if knob.kind === "scalar"}
              <FxSlider
                param={slider(knob)}
                {readOnly}
                onchange={(norm) => onscalar(knob.target, norm)}
              />
            {:else}
              <div class="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)] items-center gap-3">
                <!-- `aria-label` rather than `<label for>`: a param name is
                     unique within its group but not across the panel. -->
                <span class="truncate font-mono text-xs text-[var(--ink-dim)]">{knob.name}</span>
                <select
                  aria-label={knob.name}
                  disabled={readOnly}
                  value={typeof knob.value === "string" ? knob.value : ""}
                  onchange={(e) => onchoice(knob.target, e.currentTarget.value)}
                  class="min-h-11 rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
                         px-2 font-mono text-xs disabled:opacity-40
                         focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                >
                  {#each knob.choices ?? [] as choice (choice)}
                    <option value={choice}>{choice}</option>
                  {/each}
                </select>
              </div>
            {/if}
          {/each}
        </div>
      </section>
    {/each}
  </div>
{/if}
