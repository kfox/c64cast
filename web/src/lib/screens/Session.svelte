<script lang="ts">
  import { onMount } from "svelte";

  import { api } from "$lib/api";
  import { fetchLibrary, launch, PreflightRefused, withToggledFavorite } from "$lib/actions";
  import Button from "$lib/components/Button.svelte";
  import Diagnostics from "$lib/components/Diagnostics.svelte";
  import StateBadge from "$lib/components/StateBadge.svelte";
  import ViewerLink from "$lib/components/ViewerLink.svelte";
  import { refDisplayLabel } from "$lib/configListLogic";
  import type { Console } from "$lib/console.svelte";
  import { describeError } from "$lib/errorsLogic";
  import type { Router } from "$lib/router.svelte";
  import type { LibraryState, ValidationReport } from "$lib/types";

  interface Props {
    host: Console;
    router: Router;
    /** The config this browser has picked, shared with every other tab —
     *  owned by the shell (`App.svelte`) rather than local state, so a config
     *  picked here is still selected after a trip to the Editor and back. */
    selected: string;
    onselect: (ref: string) => void;
  }

  let { host, router, selected, onselect }: Props = $props();

  let library = $state<LibraryState | null>(null);
  let problem = $state("");
  // A pre-flight refusal's full report, alongside `problem`'s one-line
  // summary — set only when the failure is one of those, cleared by every
  // other action so a stale report doesn't survive past it.
  let report = $state<ValidationReport | null>(null);
  // Set while an action is in flight. The supervisor answers 202 the instant
  // it claims the transition, so this only covers the request itself — what
  // happens next is the state feed's job, and the badge is where it shows.
  let sending = $state(false);

  // The shell's tab-bar Start button has nowhere of its own to show a
  // refusal; it stashes one on the host and this screen claims it once, the
  // instant it's routed to.
  $effect(() => {
    if (host.launchProblem) {
      problem = host.launchProblem.message;
      report = host.launchProblem.report;
      host.launchProblem = null;
    }
  });

  const status = $derived(host.session);
  // Not `state`: a variable of that name makes `$state` read as a store
  // reference, and the rune stops resolving in this component.
  const phase = $derived(status?.state ?? "idle");
  const startable = $derived(phase === "idle" || phase === "error");
  const running = $derived(phase === "running");
  const busy = $derived(sending || phase === "starting" || phase === "stopping");

  onMount(() => {
    void refreshLibrary();
  });

  async function refreshLibrary(): Promise<void> {
    try {
      library = await fetchLibrary();
    } catch (e) {
      problem = describeError(e);
      report = null;
    }
  }

  async function act(fn: () => Promise<unknown>): Promise<void> {
    problem = "";
    report = null;
    sending = true;
    try {
      await fn();
    } catch (e) {
      problem = describeError(e);
      report = e instanceof PreflightRefused ? e.report : null;
    } finally {
      sending = false;
    }
  }

  const displayName = refDisplayLabel;

  async function toggleFavorite(ref: string, on: boolean): Promise<void> {
    try {
      library = await withToggledFavorite(library, ref, on);
    } catch (e) {
      problem = describeError(e);
      report = null;
    }
  }

  async function quickLaunch(ref: string): Promise<void> {
    onselect(ref);
    await act(() => launch(host, ref));
    await refreshLibrary();
  }
</script>

{#snippet configRow(ref: string, hint: string)}
  {@const isFavorite = library?.favorites.includes(ref) ?? false}
  <li class="flex items-center gap-1.5">
    <button
      onclick={() => onselect(ref)}
      ondblclick={() => void quickLaunch(ref)}
      aria-pressed={selected === ref}
      class="min-w-0 flex-1 rounded-md border px-2.5 py-2 text-left text-sm
             {selected === ref
        ? 'border-[var(--accent)] bg-[var(--panel-alt)]'
        : 'border-transparent hover:bg-[var(--panel-alt)]'}"
    >
      <span class="block truncate font-mono">{displayName(ref)}</span>
      {#if hint}
        <span class="block text-xs text-[var(--ink-dim)]">{hint}</span>
      {/if}
    </button>
    {#if !host.readOnly}
      <button
        type="button"
        aria-pressed={isFavorite}
        aria-label={isFavorite ? "Remove favorite" : "Add favorite"}
        onclick={() => void toggleFavorite(ref, !isFavorite)}
        class="min-h-9 min-w-9 shrink-0 rounded text-base leading-none
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]
               {isFavorite ? 'text-c64-yellow' : 'text-[var(--ink-dim)]'}"
      >
        {isFavorite ? "★" : "☆"}
      </button>
    {/if}
  </li>
{/snippet}

<div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]">
  <!-- `min-w-0` on every grid item, here and on the other screens: a grid item
       is min-content-sized by default, so one long log line or one unbroken
       path makes the *page* wider than the phone it is being read on and every
       screen scrolls sideways. The panels that hold wide content scroll it
       themselves; this is what lets them. -->
  <section class="panel min-w-0 p-5 lg:col-start-1">
    <header class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Session</h2>
      <StateBadge state={phase} stale={!host.connected} />
    </header>

    <dl class="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
      <dt class="text-[var(--ink-dim)]">Configuration</dt>
      <dd class="flex items-center gap-2 font-mono break-all">
        {status?.config_ref ? refDisplayLabel(status.config_ref) : status?.config_path || "—"}
        {#if status?.config_ref}
          <button
            class="shrink-0 text-xs underline underline-offset-2"
            onclick={() => router.go("config", status?.config_ref ?? "")}
          >
            Edit
          </button>
        {/if}
      </dd>

      <dt class="text-[var(--ink-dim)]">Systems</dt>
      <dd class="font-mono">{status?.systems.length ? status.systems.join(", ") : "—"}</dd>

      <dt class="text-[var(--ink-dim)]">Generation</dt>
      <dd class="font-mono">{status?.generation ?? 0}</dd>
    </dl>

    {#if (status?.hardware_wait_s ?? 0) > 0}
      <p class="mt-4 rounded-lg bg-[var(--panel-alt)] px-3 py-2 text-sm text-c64-yellow">
        Waiting for the hardware to settle ({status?.hardware_wait_s}s) — the DMA service refuses a
        new connection for a moment after one closes.
      </p>
    {/if}

    {#if status?.last_error}
      <p class="mt-4 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
        {status.last_error}
      </p>
    {/if}

    {#if problem}
      <div class="mt-4 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
        <p>{problem}</p>
        <Diagnostics diagnostics={report?.diagnostics ?? []} />
      </div>
    {/if}

    {#if host.readOnly}
      <p class="mt-4 text-sm text-[var(--ink-dim)]">
        This console holds a read-only token. It follows the host but cannot start or stop it.
      </p>
    {:else}
      <div class="mt-5 space-y-3">
        <p class="text-sm text-[var(--ink-dim)]">
          Selected:
          <span class="text-[var(--ink)]" class:font-mono={selected}>
            {selected || "nothing yet"}
          </span>
        </p>
        <div class="flex flex-wrap gap-2">
          {#if running}
            <!-- Never an implicit stop: replacing a running show is `switch`,
                 which is the one place stop → settle → start is sequenced. -->
            <Button
              variant="primary"
              disabled={busy}
              onclick={() => act(() => launch(host, selected))}
            >
              Switch to this
            </Button>
            <Button disabled={busy} onclick={() => act(() => api.reload())}>Reload scenes</Button>
            <Button variant="danger" disabled={busy} onclick={() => act(() => api.stop())}>
              Stop
            </Button>
          {:else}
            <Button
              variant="primary"
              disabled={busy || !startable}
              onclick={() => act(() => launch(host, selected))}
            >
              Start
            </Button>
            <Button disabled={busy || startable} onclick={() => act(() => api.stop())}>Stop</Button>
          {/if}
        </div>
      </div>

      <!-- Sharing the console has meant sharing the token that can stop the
           show; the read-only role existed with no way to hand one out. -->
      <div class="mt-6 border-t border-[var(--edge)] pt-4">
        <h3 class="mb-2 text-sm font-semibold tracking-wide uppercase">Share</h3>
        <ViewerLink />
      </div>
    {/if}
  </section>

  <div class="grid min-w-0 gap-4 lg:col-start-2 lg:row-start-1">
    <section class="panel min-w-0 p-5">
      <h2 class="mb-3 text-lg font-semibold">Favorites</h2>
      {#if library === null}
        <p class="text-sm text-[var(--ink-dim)]">Loading…</p>
      {:else if library.favorites.length === 0}
        <p class="text-sm text-[var(--ink-dim)]">
          None yet — star a configuration in the
          <button class="underline underline-offset-2" onclick={() => router.go("config")}>
            Editor
          </button>
          to keep it here.
        </p>
      {:else}
        <ul class="space-y-1">
          {#each library.favorites as ref (ref)}
            {@render configRow(ref, "")}
          {/each}
        </ul>
      {/if}
    </section>

    <section class="panel min-w-0 p-5">
      <h2 class="mb-3 text-lg font-semibold">Recently launched</h2>
      {#if library === null}
        <p class="text-sm text-[var(--ink-dim)]">Loading…</p>
      {:else if library.recents.length === 0}
        <p class="text-sm text-[var(--ink-dim)]">Nothing started from this host yet.</p>
      {:else}
        <ul class="space-y-1">
          {#each library.recents as entry (entry.ref)}
            {@render configRow(entry.ref, new Date(entry.at * 1000).toLocaleString())}
          {/each}
        </ul>
      {/if}
    </section>
  </div>
</div>
