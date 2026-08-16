<script lang="ts">
  import LogPane from "$lib/components/LogPane.svelte";
  import type { LogLine } from "$lib/types";

  interface Props {
    lines: LogLine[];
  }

  let { lines }: Props = $props();

  let open = $state(false);

  /** The last line, which is what the collapsed bar shows. A save refused or a
   *  scene that failed mid-show is the host's own account of what happened, and
   *  it used to live on one screen — so the reader was on Configs or Live and
   *  the explanation was a tab away. */
  const latest = $derived(lines.length ? lines[lines.length - 1] : null);

  const BAD = new Set(["ERROR", "CRITICAL"]);
  /** Errors since the drawer was last opened, so the bar can say there is a
   *  reason to open it without shouting about a log that is merely long. */
  let seen = $state(0);
  const unread = $derived(lines.filter((l) => l.seq > seen && BAD.has(l.level)).length);

  function toggle(): void {
    open = !open;
    if (open && latest) seen = latest.seq;
  }
</script>

<!-- Fixed rather than in the flow: the log is the same log on every screen, and
     a reader who wants it while a show is running should not have to leave the
     controls to read it. -->
<div class="fixed inset-x-0 bottom-0 z-10 border-t border-[var(--edge)] bg-[var(--panel)]">
  <div class="mx-auto max-w-5xl px-4 sm:px-6">
    <button
      type="button"
      onclick={toggle}
      aria-expanded={open}
      class="flex min-h-11 w-full items-center gap-3 text-start
             focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
    >
      <span class="font-mono text-xs text-[var(--ink-dim)]">log</span>
      {#if unread > 0}
        <span class="rounded-full bg-c64-red/20 px-2 font-mono text-[0.65rem] text-c64-red">
          {unread}
        </span>
      {/if}
      <span class="min-w-0 flex-1 truncate font-mono text-xs text-[var(--ink-dim)]">
        {latest ? latest.message : "nothing logged yet"}
      </span>
      <span aria-hidden="true" class="font-mono text-xs text-[var(--ink-dim)]">
        {open ? "▾" : "▴"}
      </span>
    </button>
    {#if open}
      <div class="pb-3">
        <LogPane {lines} />
      </div>
    {/if}
  </div>
</div>
