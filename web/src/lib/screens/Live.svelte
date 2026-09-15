<script lang="ts">
  import { onMount } from "svelte";

  import { api } from "$lib/api";
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
  import { refDisplayLabel } from "$lib/configListLogic";
  import type { Console } from "$lib/console.svelte";
  import { describeError } from "$lib/errorsLogic";
  import { DocIndex, documentation } from "$lib/introspect";
  import { commandForKey, commandForKeyUp, isTypingTarget } from "$lib/liveKeysLogic";
  import type { Router } from "$lib/router.svelte";

  interface Props {
    host: Console;
    router: Router;
  }

  let { host, router }: Props = $props();

  const systems = $derived(host.systems);
  // The system in the address bar, or the first one running — a name that no
  // longer matches falls back rather than showing nothing.
  const current = $derived(systems.find((s) => s.name === router.tail) ?? systems[0] ?? null);

  /** Every control is dead while the socket is down: `Console.send` drops a
   *  command into a closed socket without a word. */
  const frozen = $derived(host.readOnly || !host.connected);

  /** Which machines can show a picture, asked once per screen mount — a fact
   *  about the hardware, and asking starts no stream. */
  let screens = $state<Record<string, boolean>>({});
  const screenReady = $derived(current !== null && screens[current.name] === true);

  // The live palette, for the Tune panel's `c64color` knobs (border and
  // background) — the same cached fetch the Editor uses.
  let docs = $state<DocIndex | null>(null);

  onMount(async () => {
    try {
      screens = (await api.screen()).systems;
    } catch {
      // A host too old to know the route, or one that answered badly — the
      // panel then says this machine cannot show a picture.
      screens = {};
    }
    try {
      docs = await documentation();
    } catch {
      // No palette — the knob falls back to a <select>, still writable.
    }
  });

  function send(cmd: Record<string, unknown>): void {
    if (current === null) return;
    host.send({ ...cmd, system: current.name });
  }

  let showKeys = $state(false);

  /** Ctrl/Alt/Meta only: a plain `?` is `Shift+/` on a US layout, and Caps
   *  Lock reports the letters Shift would. */
  function hasModifier(event: KeyboardEvent): boolean {
    return event.ctrlKey || event.altKey || event.metaKey;
  }

  function fromTypingTarget(event: KeyboardEvent): boolean {
    const target = event.target;
    return (
      target instanceof HTMLElement && isTypingTarget(target.tagName, target.isContentEditable)
    );
  }

  // `[`/`]` only: which held keys actually got a press sent, so the release
  // still fires on keyup after focus has moved to a button or field —
  // otherwise the rewind or fast-forward it started never lets go.
  const heldKeys = new Set<string>();

  function onWindowKeydown(event: KeyboardEvent): void {
    // Auto-repeat would fire a one-shot verb (pause/resume, tap, a clip
    // launch) many times a second under a held key.
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
    if (event.key === "[" || event.key === "]") heldKeys.add(event.key);
    event.preventDefault();
    for (const cmd of commands) send(cmd);
  }

  function onWindowKeyup(event: KeyboardEvent): void {
    if (!heldKeys.delete(event.key)) return;
    const commands = commandForKeyUp(event.key);
    if (commands === null) return;
    event.preventDefault();
    for (const cmd of commands) send(cmd);
  }

  // The keyboard equivalent of TransportBar's `onpointercancel`: a window that
  // loses focus mid-hold (alt-tab, a browser dialog) never sees the keyup, so
  // rw/ff would run on indefinitely.
  function onWindowBlur(): void {
    for (const key of heldKeys) {
      heldKeys.delete(key);
      const commands = commandForKeyUp(key);
      if (commands !== null) for (const cmd of commands) send(cmd);
    }
  }

  let starting = $state(false);
  let problem = $state("");

  async function start(): Promise<void> {
    problem = "";
    starting = true;
    try {
      await api.start(null);
    } catch (e) {
      problem = describeError(e);
    } finally {
      starting = false;
    }
  }
</script>

<!-- Keydown bails on its own for a typing target, a modifier, a repeat, or
     nothing running; keyup and blur act only on a key this component put a
     press on — so all three are safe to keep mounted for the whole screen. -->
<svelte:window onkeydown={onWindowKeydown} onkeyup={onWindowKeyup} onblur={onWindowBlur} />

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
        {#if host.session?.config_ref || host.session?.config_path}
          <span class="truncate font-mono text-xs text-[var(--ink-dim)]">
            {host.session?.config_ref
              ? refDisplayLabel(host.session.config_ref)
              : host.session?.config_path}
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
        <span class="truncate font-mono">{refDisplayLabel(host.session.config_ref)}</span>
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

    <ScreenView system={current.name} available={screenReady} />

    <!-- `items-start` so a short clip grid does not stretch to the height of
         a long effect rack. -->
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

      <!-- Generated from what the *running scene* declares, so every control
           here writes somewhere. -->
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
