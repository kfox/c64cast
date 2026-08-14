<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  import { Console } from "$lib/console.svelte";
  import SessionScreen from "$lib/screens/Session.svelte";

  // One feed for the whole app, owned by the shell and handed down. Screens
  // added later (the performance surface, the config editor) read the same
  // frames rather than opening sockets of their own.
  const host = new Console();

  onMount(() => host.connect());
  onDestroy(() => host.close());
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

  <main class="flex-1">
    <SessionScreen {host} />
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
