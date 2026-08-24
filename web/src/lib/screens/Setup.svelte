<script lang="ts">
  import Button from "$lib/components/Button.svelte";
  import { submitSetup, waitForRestart, type SetupState } from "$lib/setup";

  interface Props {
    /** What `probeSetup` answered. The shell only mounts this screen when
     *  setup is actually pending, so there is no "nothing to do" state here. */
    setup: SetupState;
  }

  let { setup }: Props = $props();

  let connection = $state("");
  let token = $state("");
  let problem = $state("");
  // `form` until the host has accepted it, `restarting` while it rebuilds
  // itself, `ready` once it is answering again — at which point the page
  // navigates itself into the console.
  let phase = $state<"form" | "restarting" | "ready">("form");
  let loginUrl = $state("");

  const box = `min-h-11 w-full rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
               px-3 py-1 font-mono text-sm disabled:opacity-40
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]`;

  // Never the real one, even masked: the form answers anybody on the LAN
  // while the window is open, so the host reports only *that* its token is
  // fixed and this stands in for it.
  const REDACTED = "••••••••••••••••";

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    problem = "";
    phase = "restarting";
    try {
      loginUrl = await submitSetup({ connection, token: setup.token_settable ? token : "" });
    } catch (e) {
      problem = e instanceof Error ? e.message : String(e);
      phase = "form";
      return;
    }
    // The host is tearing its app down and building the next one as we ask.
    // Wait for it rather than navigating into the gap — and go anyway if it
    // takes too long, since the link below is the only way in from here.
    await waitForRestart();
    phase = "ready";
    window.location.href = loginUrl;
  }
</script>

<div class="mx-auto flex min-h-full max-w-lg flex-col justify-center gap-6 p-6">
  <header class="flex flex-col gap-1">
    <h1 class="font-mono text-xl font-semibold tracking-tight">
      c64cast
      <span class="text-[var(--ink-dim)]">setup</span>
    </h1>
    <p class="text-sm text-[var(--ink-dim)]">
      This appliance has not been configured yet. Tell it which machine to drive; everything else
      can be changed later from the console.
    </p>
  </header>

  {#if phase === "form"}
    <form class="flex flex-col gap-5" onsubmit={submit}>
      <label class="flex flex-col gap-1.5">
        <span class="text-sm font-medium">Connection target</span>
        <input
          class={box}
          type="text"
          spellcheck="false"
          autocapitalize="off"
          autocomplete="off"
          placeholder="u64://192.168.2.64"
          required
          bind:value={connection}
        />
        <span class="text-xs text-[var(--ink-dim)]">
          <code>u64://HOST</code> for an Ultimate 64 or II+, <code>tr://</code> for a TeensyROM+ on
          USB, <code>tr://HOST</code> for one over the network.
        </span>
      </label>

      <label class="flex flex-col gap-1.5">
        <span class="text-sm font-medium">Access token</span>
        <input
          class={box}
          type="password"
          autocomplete="new-password"
          disabled={!setup.token_settable}
          value={setup.token_settable ? token : REDACTED}
          oninput={(e) => (token = e.currentTarget.value)}
        />
        <span class="text-xs text-[var(--ink-dim)]">
          {#if setup.token_settable}
            Leave this blank to keep the token this host generated for itself — you will be signed
            in with it either way.
          {:else}
            This host's token is set by its configuration and cannot be changed here.
          {/if}
        </span>
      </label>

      {#if problem}
        <p class="text-sm text-c64-red" role="alert">{problem}</p>
      {/if}

      <div class="flex justify-end">
        <Button variant="primary" type="submit">Configure</Button>
      </div>
    </form>
  {:else}
    <p class="text-sm text-[var(--ink-dim)]" role="status">
      {phase === "restarting" ? "Restarting the host…" : "Opening the console…"}
    </p>
    {#if loginUrl}
      <p class="text-sm">
        <a class="underline underline-offset-2" href={loginUrl}>Open the console</a>
        if this page does not go there by itself.
      </p>
    {/if}
  {/if}
</div>
