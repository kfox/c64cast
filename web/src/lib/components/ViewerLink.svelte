<script lang="ts">
  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";

  // Held rather than fetched on mount: asking mints the token, and a console
  // that minted one every time somebody opened it would be creating a
  // credential nobody wanted.
  let url = $state("");
  let minted = $state(false);
  let problem = $state("");
  let copied = $state("");
  let busy = $state(false);

  async function reveal(): Promise<void> {
    problem = "";
    copied = "";
    busy = true;
    try {
      const link = await api.viewerLink();
      // The host answers with a path because it may be bound to 0.0.0.0 and
      // cannot know which of its addresses this browser reached it on. This
      // browser knows exactly that, and it is the address worth sharing.
      url = new URL(link.path, location.origin).toString();
      minted = link.minted;
    } catch (e) {
      problem = e instanceof ApiError || e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function copy(): Promise<void> {
    try {
      // Not available over plain HTTP outside localhost, which is exactly how
      // this host is usually reached — so the link is on screen and selectable
      // whether or not this works.
      await navigator.clipboard.writeText(url);
      copied = "Copied.";
    } catch {
      copied = "Could not reach the clipboard — select the link above instead.";
    }
  }
</script>

<div class="space-y-2">
  <p class="text-sm text-[var(--ink-dim)]">
    A read-only link follows the show and drives nothing: no start, no stop, no tuning, no config
    writes. Hand it out instead of the address you are using, which can do all four.
  </p>

  {#if url}
    <label class="block">
      <span class="sr-only">Read-only console link</span>
      <input
        readonly
        value={url}
        onfocus={(e) => e.currentTarget.select()}
        class="min-h-11 w-full rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
               px-2 py-1 font-mono text-xs
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
      />
    </label>
    <div class="flex flex-wrap items-center gap-2">
      <Button onclick={copy}>Copy link</Button>
      {#if copied}<span class="text-xs text-[var(--ink-dim)]">{copied}</span>{/if}
    </div>
    {#if minted}
      <p class="text-xs text-c64-yellow">
        This is a new token, and it now works until you replace it. Anyone holding the link can
        watch the show.
      </p>
    {/if}
  {:else}
    <Button disabled={busy} onclick={reveal}>Get a read-only link</Button>
  {/if}

  {#if problem}
    <p class="text-sm text-c64-red">{problem}</p>
  {/if}
</div>
