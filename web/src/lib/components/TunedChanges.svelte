<script lang="ts">
  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import type { TuneChange, TunedState } from "$lib/types";

  interface Props {
    tuned: TunedState;
    system: string;
    readOnly?: boolean;
  }

  let { tuned, system, readOnly = false }: Props = $props();

  let busy = $state(false);
  let problem = $state("");
  let done = $state("");

  const savable = $derived(tuned.savable > 0 && tuned.config_path !== "");
  const file = $derived(tuned.config_path.split(/[\\/]/).pop() ?? "");
  const lost = $derived(tuned.changes.length - tuned.savable);

  /** The answer has to outlive the thing it answers: a save empties the record,
   *  and a section that only showed while the record was there would take the
   *  confirmation with it on the very next state push. */
  const showing = $derived(tuned.changes.length > 0 || done !== "" || problem !== "");

  /** The host's own rounding, so a value reads the same here as it does in the
   *  log line and the exit prompt. */
  function show(value: number | string | null): string {
    if (typeof value === "number") return String(Number(value.toPrecision(3)));
    return value === null ? "—" : value;
  }

  /** The knob's own name; the holder prefix is the same for every row in the
   *  list and repeating it 8 times says nothing. */
  function knob(change: TuneChange): string {
    return change.target.split(".").slice(1).join(".") || change.target;
  }

  async function run(save: boolean): Promise<void> {
    problem = "";
    done = "";
    busy = true;
    try {
      if (save) {
        const out = await api.saveLiveTune(system);
        done = `Saved ${out.saved.length} to ${out.path}${out.backup ? " (previous kept alongside it)" : ""}.`;
      } else {
        const out = await api.discardLiveTune(system);
        done = `Dropped ${out.discarded}. The show still sounds and looks the same — only the offer is gone.`;
      }
    } catch (e) {
      problem = e instanceof ApiError || e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if showing}
  <!-- Below the knobs, because this is the record of turning them. The show is
       already playing these values; what is at stake here is only whether the
       next run starts from them. -->
  <section class="mt-4 rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)] p-3">
    <h3 class="mb-2 text-xs font-semibold tracking-wide text-[var(--ink-dim)] uppercase">
      Tuned this run
    </h3>

    <ul class="space-y-1">
      {#each tuned.changes as change (change.target)}
        <li class="flex items-baseline gap-2 font-mono text-xs">
          <span class="flex-1 truncate {change.field === null ? 'text-[var(--ink-dim)]' : ''}">
            {knob(change)}
          </span>
          <span class="text-[var(--ink-dim)]">{show(change.old)} →</span>
          <span class="tabular-nums">{show(change.new)}</span>
          {#if change.field === null}
            <span class="text-[0.65rem] text-c64-yellow">runtime only</span>
          {/if}
        </li>
      {/each}
    </ul>

    {#if lost > 0}
      <p class="mt-2 text-xs text-[var(--ink-dim)]">
        {lost}
        {lost === 1 ? "change has" : "changes have"} no config field behind
        {lost === 1 ? "it" : "them"} — a palette mode belongs to its scene rather than to
        <code class="font-mono">[color]</code>, so {lost === 1 ? "it ends" : "they end"} with the show.
      </p>
    {/if}

    {#if tuned.snippet}
      <p class="mt-2 text-xs text-[var(--ink-dim)]">
        This run has no config file to keep them in. Paste this into one:
      </p>
      <pre
        class="mt-1 overflow-x-auto rounded border border-[var(--edge)] p-2 font-mono text-xs">{tuned.snippet}</pre>
    {/if}

    {#if !readOnly && tuned.changes.length > 0}
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <!-- Absent rather than disabled when there is nothing to write: a
             greyed "Keep 0 in the config" is a worse answer than the sentence
             above it, which says why. -->
        {#if savable}
          <Button
            variant="primary"
            disabled={busy}
            title="Write [color] into {file}"
            onclick={() => run(true)}
          >
            Keep {tuned.savable} in {file}
          </Button>
        {/if}
        <Button disabled={busy} onclick={() => run(false)}>Discard</Button>
      </div>
    {/if}

    {#if problem}
      <p class="mt-2 rounded border border-c64-red/50 px-2 py-1 text-xs text-c64-red">{problem}</p>
    {:else if done}
      <p class="mt-2 text-xs text-c64-green">{done}</p>
    {/if}
  </section>
{/if}
