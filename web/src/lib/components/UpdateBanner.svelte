<script lang="ts">
  import { onMount } from "svelte";

  import { api } from "$lib/api";
  import type { UpdateState } from "$lib/types";
  import {
    browserStore,
    DISMISSED_KEY,
    isDismissed,
    isPending,
    isStale,
    isStaleDismissed,
    readDismissed,
    STALE_DISMISSED_KEY,
    writeDismissed,
  } from "$lib/updateBannerLogic";

  const store = browserStore();
  // Read once, at load: a banner must not appear under the operator's hands
  // mid-show.
  const openedAt = Date.now();

  let updateState = $state<UpdateState | null>(null);
  let dismissedVersion = $state<string | null>(readDismissed(store, DISMISSED_KEY));
  let staleDismissed = $state<string | null>(readDismissed(store, STALE_DISMISSED_KEY));

  onMount(async () => {
    try {
      updateState = await api.update();
    } catch {
      // Silent: the screens already report an unreachable host.
    }
  });

  const showUpgrade = $derived(
    isPending(updateState) && !isDismissed(updateState, dismissedVersion),
  );
  const showStale = $derived(
    !isPending(updateState) &&
      isStale(updateState, openedAt) &&
      !isStaleDismissed(updateState, staleDismissed),
  );

  function dismiss(): void {
    if (showUpgrade && updateState?.latest_version) {
      dismissedVersion = updateState.latest_version;
      writeDismissed(store, DISMISSED_KEY, updateState.latest_version);
      return;
    }
    if (updateState?.running_version) {
      staleDismissed = updateState.running_version;
      writeDismissed(store, STALE_DISMISSED_KEY, updateState.running_version);
    }
  }
</script>

{#if showUpgrade || showStale}
  <div
    class="flex items-center justify-between gap-3 rounded border px-3 py-1.5 text-xs
           {showUpgrade
      ? 'border-c64-yellow/50 bg-c64-yellow/10'
      : 'border-[var(--edge)] bg-[var(--panel-alt)] text-[var(--ink-dim)]'}"
  >
    {#if showUpgrade}
      <p>
        A newer c64cast release is available:
        <span class="font-mono">{updateState?.latest_version}</span>
        (running <span class="font-mono">{updateState?.running_version}</span>). Upgrade with
        <code class="font-mono">c64cast --upgrade</code>.
      </p>
    {:else}
      <!-- Word for word what `update_state.motd_line` says, and for its
           reason: `is_stale` falls back to `checked_at` when nothing has ever
           gone unanswered, and in that branch the last attempt did answer. All
           that is true in both branches is that no check has succeeded. -->
      <p>
        No update check has succeeded in over {updateState?.stale_after_days} days: this machine
        cannot say whether c64cast
        <span class="font-mono">{updateState?.running_version}</span>
        is still current. Check with
        <code class="font-mono">c64cast --check-for-updates</code>.
      </p>
    {/if}
    <button
      type="button"
      class="shrink-0 text-[var(--ink-dim)] hover:text-[var(--ink)]"
      onclick={dismiss}
      aria-label={showUpgrade ? "Dismiss update notice" : "Dismiss until the next upgrade"}
    >
      ✕
    </button>
  </div>
{/if}
