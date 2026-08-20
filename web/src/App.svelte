<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import { ApiError } from "$lib/api";
  import { launch } from "$lib/actions";
  import { Console } from "$lib/console.svelte";
  import Button from "$lib/components/Button.svelte";
  import LogDrawer from "$lib/components/LogDrawer.svelte";
  import { drafts } from "$lib/drafts.svelte";
  import { Router, type Screen } from "$lib/router.svelte";
  import ConfigScreen from "$lib/screens/Config.svelte";
  import LiveScreen from "$lib/screens/Live.svelte";
  import SessionScreen from "$lib/screens/Session.svelte";

  // One feed for the whole app, owned by the shell and handed down. Screens
  // added later (the performance surface) read the same frames rather than
  // opening sockets of their own.
  const host = new Console();
  const router = new Router();

  onMount(() => host.connect());
  onDestroy(() => {
    host.close();
    router.dispose();
  });

  const tabs: { screen: Screen; label: string }[] = [
    { screen: "session", label: "Session" },
    { screen: "live", label: "Live" },
    { screen: "config", label: "Editor" },
  ];

  // Unsaved edits are marked on the tab, not just inside the screen that holds
  // them: the file list and the file header both say so, and neither is on
  // screen once somebody has walked away to watch the show.
  const unsaved = $derived(drafts.count);

  // The one config every tab shares. Defaults to whatever the host was
  // launched with (`config_ref`, the only "host default" concept left) the
  // instant the state feed says so, and otherwise follows whatever was picked
  // on the Session screen or opened in the Editor — so a Start button is
  // always reachable from wherever the reader happens to be.
  let selectedConfig = $state("");
  let starting = $state(false);

  $effect(() => {
    if (!selectedConfig && host.session?.config_ref) selectedConfig = host.session.config_ref;
  });

  // Opening a file in the Editor is picking it, the same as clicking it in
  // the Session list — both are "this is the show I'm working on right now".
  $effect(() => {
    if (router.screen === "config" && router.tail) selectedConfig = router.tail;
  });

  // A start or switch this browser asked for lands on the Live tab once the
  // show is actually up — not before, and not for a transition somebody else
  // drove from another console.
  $effect(() => {
    if (host.expectingStart && host.session?.state === "running") {
      host.expectingStart = false;
      router.go("live");
    }
  });

  const phase = $derived(host.session?.state ?? "idle");
  const running = $derived(phase === "running");
  const busy = $derived(starting || phase === "starting" || phase === "stopping");
  const canQuickStart = $derived(!host.readOnly && selectedConfig !== "" && !busy);

  async function quickStart(): Promise<void> {
    starting = true;
    try {
      await launch(host, selectedConfig);
    } catch (e) {
      // Surfaced on the Session screen's own log/status rather than repeated
      // here — a toast on the shell would have nowhere permanent to live.
      console.error(e instanceof ApiError ? e.message : e);
    } finally {
      starting = false;
    }
  }
</script>

<div class="mx-auto flex min-h-full max-w-5xl flex-col gap-4 p-4 sm:p-6">
  <header class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
    <h1 class="font-mono text-xl font-semibold tracking-tight">
      c64cast
      <span class="text-[var(--ink-dim)]">console</span>
    </h1>
    <p class="font-mono text-xs text-[var(--ink-dim)]">
      {#if host.connected}
        connected
      {:else if host.ready}
        reconnecting…
      {:else}
        connecting…
      {/if}
      {#if host.readOnly}
        · read-only
      {/if}
    </p>
  </header>

  <nav class="flex flex-wrap items-center gap-1 border-b border-[var(--edge)]" aria-label="Screens">
    {#each tabs as tab (tab.screen)}
      <button
        onclick={() => router.go(tab.screen)}
        aria-current={router.screen === tab.screen ? "page" : undefined}
        class="-mb-px min-h-11 border-b-2 px-4 text-sm font-medium
               {router.screen === tab.screen
          ? 'border-[var(--accent)] text-[var(--ink)]'
          : 'border-transparent text-[var(--ink-dim)] hover:text-[var(--ink)]'}"
      >
        {tab.label}
        {#if tab.screen === "config" && unsaved > 0}
          <span
            class="ms-1 inline-block size-1.5 rounded-full bg-c64-yellow align-middle"
            title="{unsaved} file{unsaved === 1 ? '' : 's'} with unsaved edits"
          ></span>
        {/if}
      </button>
    {/each}

    <!-- Reachable from every tab, not just the Session screen's own — the
         point is that a config file selected anywhere is one click from
         running, with no detour back to Session first. -->
    {#if !host.readOnly}
      <span class="ms-auto mb-1">
        <Button
          variant="primary"
          disabled={!canQuickStart}
          title={selectedConfig || "Pick a configuration first"}
          onclick={quickStart}
        >
          {running ? "Switch to" : "Start"}
          {#if selectedConfig}
            <span class="max-w-32 truncate font-mono">{selectedConfig}</span>
          {/if}
        </Button>
      </span>
    {/if}
  </nav>

  <!-- `pb-14` clears the log drawer's collapsed bar, which is fixed to the
       bottom of the viewport and would otherwise sit on the last control. -->
  <main class="flex-1 pb-14">
    {#if router.screen === "config"}
      <ConfigScreen {host} {router} />
    {:else if router.screen === "live"}
      <LiveScreen {host} {router} />
    {:else}
      <SessionScreen {host} {router} selected={selectedConfig} onselect={(ref) => (selectedConfig = ref)} />
    {/if}
  </main>

  <footer class="text-xs text-[var(--ink-dim)]">
    <a class="underline underline-offset-2" href="/perf">Performance console</a>
    <span aria-hidden="true"> · </span>
    <a
      class="underline underline-offset-2"
      href="https://kfox.github.io/c64cast/reference/07-inputs-and-outputs/"
      rel="noreferrer">Documentation</a
    >
  </footer>
</div>

<LogDrawer lines={host.log} />
