<script lang="ts">
  import ClipGrid from "$lib/components/ClipGrid.svelte";
  import EffectRack from "$lib/components/EffectRack.svelte";
  import LookPads from "$lib/components/LookPads.svelte";
  import TempoBar from "$lib/components/TempoBar.svelte";
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
</script>

{#if current === null}
  <section class="panel p-5">
    <h2 class="mb-2 text-lg font-semibold">Live</h2>
    <p class="text-sm text-[var(--ink-dim)]">
      {#if !host.ready}
        Waiting for the host…
      {:else}
        Nothing is running. Start a show from the
        <button class="underline underline-offset-2" onclick={() => router.go("session")}>
          Session
        </button>
        screen and the performance surface appears here.
      {/if}
    </p>
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
        readOnly={frozen}
        ontap={() => send({ action: "tap" })}
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
      <section class="panel p-5">
        <h2 class="mb-3 text-lg font-semibold">Clips</h2>
        <ClipGrid
          clips={current.clips}
          readOnly={frozen}
          onpress={(slot, pressed) => send({ action: "launch", slot, pressed })}
        />
      </section>

      <section class="panel p-5">
        <h2 class="mb-3 text-lg font-semibold">Effects</h2>
        <EffectRack
          effects={current.effects}
          readOnly={frozen}
          onbypass={(layer, enabled) => send({ action: "fx", layer, enabled })}
          onparam={(layer, param, value) => send({ action: "fx", layer, param, value })}
        />
      </section>

      <section class="panel p-5 lg:col-span-2">
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
