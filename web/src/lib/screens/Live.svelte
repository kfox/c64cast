<script lang="ts">
  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import ClipGrid from "$lib/components/ClipGrid.svelte";
  import EffectRack from "$lib/components/EffectRack.svelte";
  import LookPads from "$lib/components/LookPads.svelte";
  import SceneList from "$lib/components/SceneList.svelte";
  import TempoBar from "$lib/components/TempoBar.svelte";
  import TunePanel from "$lib/components/TunePanel.svelte";
  import TunedChanges from "$lib/components/TunedChanges.svelte";
  import type { Console } from "$lib/console.svelte";
  import type { Router } from "$lib/router.svelte";

  interface Props {
    host: Console;
    router: Router;
  }

  let { host, router }: Props = $props();

  const systems = $derived(host.systems);
  // The system in the address bar, or the first one running. An ensemble's
  // second machine is worth being able to bookmark from a phone; a name that no
  // longer matches (the show changed underneath the link) falls back rather
  // than showing nothing.
  const current = $derived(systems.find((s) => s.name === router.tail) ?? systems[0] ?? null);

  /** Every control is dead while the socket is down, because a command sent
   *  into a closed socket is dropped without a word — better to show it. */
  const frozen = $derived(host.readOnly || !host.connected);

  function send(cmd: Record<string, unknown>): void {
    if (current === null) return;
    host.send({ ...cmd, system: current.name });
  }

  // Starting a show from here is a shortcut back to the Session screen's own
  // button, kept because "nothing is running" and "start something" are one
  // gesture apart in intent and were two screens apart in fact.
  let starting = $state(false);
  let problem = $state("");

  async function start(): Promise<void> {
    problem = "";
    starting = true;
    try {
      await api.start(null);
    } catch (e) {
      problem = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
    } finally {
      starting = false;
    }
  }
</script>

{#if current === null}
  <section class="panel p-5">
    <h2 class="mb-2 text-lg font-semibold">Live</h2>
    <p class="text-sm text-[var(--ink-dim)]">
      {#if !host.ready}
        Waiting for the host…
      {:else}
        Nothing is running, so there is nothing to drive. Start the host's own configuration here,
        or pick a different one on the
        <button class="underline underline-offset-2" onclick={() => router.go("session")}>
          Session
        </button>
        screen.
      {/if}
    </p>
    {#if host.ready && !host.readOnly}
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <Button variant="primary" disabled={starting || host.busy} onclick={start}>
          Start the host default
        </Button>
        {#if host.session?.config_path}
          <span class="truncate font-mono text-xs text-[var(--ink-dim)]">
            {host.session.config_path}
          </span>
        {/if}
      </div>
    {/if}
    {#if problem}
      <p class="mt-3 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
        {problem}
      </p>
    {/if}
  </section>
{:else}
  <div class="space-y-4">
    {#if systems.length > 1}
      <nav class="flex flex-wrap gap-1" aria-label="Systems">
        {#each systems as system (system.name)}
          <button
            onclick={() => router.go("live", system.name)}
            aria-current={system.name === current.name ? "true" : undefined}
            class="min-h-9 rounded-lg border px-3 font-mono text-xs
                   {system.name === current.name
              ? 'border-[var(--accent)] text-[var(--ink)]'
              : 'border-[var(--edge)] text-[var(--ink-dim)]'}"
          >
            {system.name}
          </button>
        {/each}
      </nav>
    {/if}

    <section class="panel p-4" class:opacity-60={!host.connected}>
      <TempoBar
        tempo={current.tempo}
        scene={current.current_scene}
        armed={current.armed}
        paused={current.paused}
        readOnly={frozen}
        ontap={() => send({ action: "tap" })}
        ontransport={(verb) => send({ action: "transport", verb })}
      />
    </section>

    {#if host.readOnly}
      <p class="text-sm text-[var(--ink-dim)]">
        This console holds a read-only token. It follows the show but cannot drive it.
      </p>
    {:else if !host.connected}
      <p class="text-sm text-c64-yellow">
        Reconnecting — the state below is the last frame that arrived.
      </p>
    {/if}

    <!-- `items-start` so a short clip grid does not stretch to the height of a
         long effect rack, which on a two-effect show is most of the panel. -->
    <div class="grid items-start gap-4 lg:grid-cols-2">
      <section class="panel min-w-0 p-5">
        <h2 class="mb-3 text-lg font-semibold">Clips</h2>
        <ClipGrid
          clips={current.clips}
          readOnly={frozen}
          onpress={(slot, pressed) => send({ action: "launch", slot, pressed })}
        />
      </section>

      <section class="panel min-w-0 p-5">
        <h2 class="mb-3 text-lg font-semibold">Effects</h2>
        <EffectRack
          effects={current.effects}
          readOnly={frozen}
          onbypass={(layer, enabled) => send({ action: "fx", layer, enabled })}
          onparam={(layer, param, value) => send({ action: "fx", layer, param, value })}
        />
      </section>

      <!-- The color pipeline, the generator and the scope: the knobs a MIDI
           controller and the on-C64 menu have always reached and the browser
           did not. Generated from what the *running scene* declares, so every
           control here writes somewhere. -->
      <section class="panel min-w-0 p-5 lg:col-span-2">
        <h2 class="mb-3 text-lg font-semibold">Tune</h2>
        <TunePanel
          knobs={current.live}
          readOnly={frozen}
          onscalar={(target, norm) => send({ action: "live", target, norm })}
          onchoice={(target, value) => send({ action: "live", target, value })}
        />
        <TunedChanges tuned={current.tuned} system={current.name} readOnly={frozen} />
      </section>

      <section class="panel min-w-0 p-5">
        <h2 class="mb-3 text-lg font-semibold">Scenes</h2>
        <SceneList
          scenes={current.scenes}
          readOnly={frozen}
          onjump={(index) => send({ action: "jump", index })}
        />
      </section>

      <section class="panel min-w-0 p-5">
        <h2 class="mb-3 text-lg font-semibold">Looks</h2>
        <LookPads
          looks={current.looks}
          readOnly={frozen}
          onlook={(slot, save) => send({ action: "look", slot, save })}
        />
      </section>
    </div>
  </div>
{/if}
