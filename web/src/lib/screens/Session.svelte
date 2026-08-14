<script lang="ts">
  import { onMount } from "svelte";

  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import ConfigList from "$lib/components/ConfigList.svelte";
  import LogPane from "$lib/components/LogPane.svelte";
  import StateBadge from "$lib/components/StateBadge.svelte";
  import type { Console } from "$lib/console.svelte";
  import type { ConfigIndex } from "$lib/types";

  interface Props {
    host: Console;
  }

  let { host }: Props = $props();

  let index = $state<ConfigIndex | null>(null);
  let chosen = $state("");
  let problem = $state("");
  // Set while an action is in flight. The supervisor answers 202 the instant
  // it claims the transition, so this only covers the request itself — what
  // happens next is the state feed's job, and the badge is where it shows.
  let sending = $state(false);

  const status = $derived(host.session);
  // Not `state`: a variable of that name makes `$state` read as a store
  // reference, and the rune stops resolving in this component.
  const phase = $derived(status?.state ?? "idle");
  const startable = $derived(phase === "idle" || phase === "error");
  const running = $derived(phase === "running");
  const busy = $derived(sending || phase === "starting" || phase === "stopping");

  onMount(() => {
    void refreshIndex();
  });

  async function refreshIndex(): Promise<void> {
    try {
      index = await api.configs();
    } catch (e) {
      problem = describe(e);
    }
  }

  function describe(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.status === 403) return `Not allowed: ${e.message}`;
      if (e.status === 409) return `The host is busy: ${e.message}`;
      if (e.status === 422) return `That configuration will not run: ${e.message}`;
      return e.message;
    }
    return e instanceof Error ? e.message : String(e);
  }

  async function act(fn: () => Promise<unknown>): Promise<void> {
    problem = "";
    sending = true;
    try {
      await fn();
    } catch (e) {
      problem = describe(e);
    } finally {
      sending = false;
    }
  }

  const ref = $derived(chosen || null);
  const label = $derived(chosen || "the host default");
</script>

<div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]">
  <section class="panel p-5 lg:col-start-1">
    <header class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Session</h2>
      <StateBadge state={phase} stale={!host.connected} />
    </header>

    <dl class="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
      <dt class="text-[var(--ink-dim)]">Configuration</dt>
      <dd class="font-mono break-all">{status?.config_path || "—"}</dd>

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
      <p class="mt-4 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
        {problem}
      </p>
    {/if}

    {#if host.readOnly}
      <p class="mt-4 text-sm text-[var(--ink-dim)]">
        This console holds a read-only token. It follows the host but cannot start or stop it.
      </p>
    {:else}
      <div class="mt-5 space-y-3">
        <p class="text-sm text-[var(--ink-dim)]">
          Selected: <span class="text-[var(--ink)]" class:font-mono={chosen}>{label}</span>
        </p>
        <div class="flex flex-wrap gap-2">
          {#if running}
            <!-- Never an implicit stop: replacing a running show is `switch`,
                 which is the one place stop → settle → start is sequenced. -->
            <Button variant="primary" disabled={busy} onclick={() => act(() => api.switch(ref))}>
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
              onclick={() => act(() => api.start(ref))}
            >
              Start
            </Button>
            <Button disabled={busy || startable} onclick={() => act(() => api.stop())}>Stop</Button>
          {/if}
        </div>
      </div>
    {/if}
  </section>

  <section class="panel p-5 lg:col-start-2 lg:row-start-1">
    <header class="mb-3 flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Configurations</h2>
      <Button onclick={refreshIndex}>Refresh</Button>
    </header>
    {#if index === null}
      <p class="text-sm text-[var(--ink-dim)]">Loading…</p>
    {:else}
      <ConfigList {index} value={chosen} onselect={(ref) => (chosen = ref)} />
    {/if}
  </section>

  <section class="panel p-5 lg:col-span-2">
    <h2 class="mb-3 text-lg font-semibold">Log</h2>
    <LogPane lines={host.log} />
  </section>
</div>
