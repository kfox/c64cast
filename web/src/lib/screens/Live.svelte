<script lang="ts">
  import { onMount } from "svelte";

  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import ClipGrid from "$lib/components/ClipGrid.svelte";
  import EffectRack from "$lib/components/EffectRack.svelte";
  import LookPads from "$lib/components/LookPads.svelte";
  import SceneList from "$lib/components/SceneList.svelte";
  import ScreenView from "$lib/components/ScreenView.svelte";
  import TempoBar from "$lib/components/TempoBar.svelte";
  import TransportBar from "$lib/components/TransportBar.svelte";
  import TunePanel from "$lib/components/TunePanel.svelte";
  import TunedChanges from "$lib/components/TunedChanges.svelte";
  import type { Console } from "$lib/console.svelte";
  import { DocIndex, documentation } from "$lib/introspect";
  import { commandForKey, commandForKeyUp, isTypingTarget } from "$lib/liveKeysLogic";
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

  /** Which machines can show a picture, asked once per screen mount. It is a
   *  fact about the hardware, so it cannot change while the host is up — and
   *  asking starts nothing, since the stream comes up when the `<img>` opens
   *  and goes down when it closes. */
  let screens = $state<Record<string, boolean>>({});
  const screenReady = $derived(current !== null && screens[current.name] === true);

  // The live palette, for the Tune panel's `c64color` knobs (border/
  // background, Live DJ/VJ Phase 7) — same cached fetch the Editor uses, so
  // opening Live first costs one request and opening it second costs none.
  let docs = $state<DocIndex | null>(null);

  onMount(async () => {
    try {
      screens = (await api.screen()).systems;
    } catch {
      // A host too old to know the route, or one that answered badly: the
      // panel then says the machine cannot show a picture, which is true of
      // this pairing even if not of the machine.
      screens = {};
    }
    try {
      docs = await documentation();
    } catch {
      // No palette yet — the knob falls back to a <select>, still writable.
    }
  });

  function send(cmd: Record<string, unknown>): void {
    if (current === null) return;
    host.send({ ...cmd, system: current.name });
  }

  // Off by default: a shortcut nobody knows about isn't a feature, but a list
  // open by default on every visit is clutter for anyone who already does.
  let showKeys = $state(false);

  /** Ctrl/Alt/Meta only — Shift stays out, or a plain `?` (`Shift+/` on a US
   *  layout) could never reach the help toggle, and Caps Lock would read as
   *  a shortcut for no reason. */
  function hasModifier(event: KeyboardEvent): boolean {
    return event.ctrlKey || event.altKey || event.metaKey;
  }

  function fromTypingTarget(event: KeyboardEvent): boolean {
    const target = event.target;
    return (
      target instanceof HTMLElement && isTypingTarget(target.tagName, target.isContentEditable)
    );
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    // Auto-repeat would spam a one-shot verb (pause/resume, tap, a clip
    // launch) many times a second while a key is just held down.
    if (event.repeat || fromTypingTarget(event) || current === null) return;
    if (event.key === "?") {
      event.preventDefault();
      showKeys = !showKeys;
      return;
    }
    const commands = commandForKey(event.key, hasModifier(event), {
      readOnly: frozen,
      paused: current.paused,
      videoFrozen: current.transport?.frozen ?? null,
      clips: current.clips,
    });
    if (commands === null) return;
    event.preventDefault();
    for (const cmd of commands) send(cmd);
  }

  function onWindowKeyup(event: KeyboardEvent): void {
    if (fromTypingTarget(event)) return;
    const commands = commandForKeyUp(event.key);
    if (commands === null) return;
    event.preventDefault();
    for (const cmd of commands) send(cmd);
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

<!-- The app's first global key handler. Both handlers bail on their own for a
     typing target, a modifier, a repeat, or nothing running — so this is safe
     to keep mounted for the whole screen rather than only once a show is up. -->
<svelte:window onkeydown={onWindowKeydown} onkeyup={onWindowKeyup} />

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
        {#if host.session?.config_ref}
          <button
            class="text-xs underline underline-offset-2"
            onclick={() => router.go("config", host.session?.config_ref ?? "")}
          >
            Edit
          </button>
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
    <div class="flex flex-wrap items-center gap-2 text-xs text-[var(--ink-dim)]">
      {#if host.session?.config_ref}
        <span class="truncate font-mono">{host.session.config_ref}</span>
        <button
          class="underline underline-offset-2"
          onclick={() => router.go("config", host.session?.config_ref ?? "")}
        >
          Edit
        </button>
      {/if}
      <button
        class="ms-auto underline underline-offset-2"
        aria-expanded={showKeys}
        onclick={() => (showKeys = !showKeys)}
      >
        Keys
      </button>
    </div>

    {#if showKeys}
      <section class="panel p-4 text-xs">
        <h2 class="mb-2 text-sm font-semibold">Keyboard shortcuts</h2>
        <p class="mb-2 text-[var(--ink-dim)]">
          Live everywhere on this screen except while a text field, a select or a button has the
          caret or the focus.
        </p>
        <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono">
          <dt>Space</dt>
          <dd class="font-sans">Pause / resume</dd>
          <dt>t</dt>
          <dd class="font-sans">Tap tempo</dd>
          <dt>n</dt>
          <dd class="font-sans">Skip to the next scene</dd>
          <dt>f</dt>
          <dd class="font-sans">Freeze / unfreeze the video</dd>
          <dt>l</dt>
          <dd class="font-sans">Toggle the A/B loop</dd>
          <dt>[ / ]</dt>
          <dd class="font-sans">Rewind / fast-forward, held</dd>
          <dt>1–8</dt>
          <dd class="font-sans">Launch that clip slot</dd>
          <dt>?</dt>
          <dd class="font-sans">Show or hide this list</dd>
        </dl>
      </section>
    {/if}
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
      />
    </section>

    {#if current.transport}
      <section class="panel p-4">
        <h2 class="mb-3 text-lg font-semibold">Transport</h2>
        <TransportBar
          transport={current.transport}
          readOnly={frozen}
          onverb={(verb, extra) => send({ action: "transport", verb, ...extra })}
        />
      </section>
    {/if}

    {#if host.readOnly}
      <p class="text-sm text-[var(--ink-dim)]">
        This console holds a read-only token. It follows the show but cannot drive it.
      </p>
    {:else if !host.connected}
      <p class="text-sm text-c64-yellow">
        Reconnecting — the state below is the last frame that arrived.
      </p>
    {/if}

    <!-- Above the controls, because it is what the controls are *for*: every
         other panel here changes something you could until now only verify by
         looking at the television. -->
    <ScreenView system={current.name} available={screenReady} />

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
          palette={docs?.palette ?? []}
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
