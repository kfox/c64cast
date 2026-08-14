<script lang="ts">
  import type { LogLine } from "$lib/types";

  interface Props {
    lines: LogLine[];
  }

  let { lines }: Props = $props();

  let box: HTMLDivElement | undefined = $state();
  // Follow the tail, but stop the moment the reader scrolls up — a log that
  // yanks itself back to the bottom while someone is reading the failure that
  // just scrolled past is the reason the daemon has a log pane at all.
  let pinned = $state(true);

  function onScroll(): void {
    if (!box) return;
    pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }

  $effect(() => {
    lines.length;
    if (pinned && box) box.scrollTop = box.scrollHeight;
  });

  const tone: Record<string, string> = {
    ERROR: "text-c64-red",
    CRITICAL: "text-c64-red",
    WARNING: "text-c64-yellow",
    DEBUG: "text-[var(--ink-dim)]",
  };

  function clock(t: number): string {
    return new Date(t * 1000).toLocaleTimeString(undefined, { hour12: false });
  }
</script>

<div class="relative">
  <div
    bind:this={box}
    onscroll={onScroll}
    class="h-64 overflow-y-auto overflow-x-auto rounded-lg bg-[var(--panel-alt)]
           p-3 font-mono text-xs leading-relaxed"
    role="log"
    aria-label="Host log"
  >
    {#if lines.length === 0}
      <p class="text-[var(--ink-dim)]">Nothing logged yet.</p>
    {/if}
    {#each lines as line (line.seq)}
      <div class="whitespace-pre">
        <span class="text-[var(--ink-dim)]">{clock(line.t)}</span>
        <span class={tone[line.level] ?? ""}>{line.message}</span>
      </div>
    {/each}
  </div>
  {#if !pinned}
    <button
      class="absolute right-3 bottom-3 rounded-full border border-[var(--edge)]
             bg-[var(--panel)] px-3 py-1 text-xs"
      onclick={() => {
        pinned = true;
        if (box) box.scrollTop = box.scrollHeight;
      }}
    >
      Jump to latest
    </button>
  {/if}
</div>
