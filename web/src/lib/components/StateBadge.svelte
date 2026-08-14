<script lang="ts">
  import type { SessionState } from "$lib/types";

  interface Props {
    state: SessionState;
    /** Dimmed when the socket is down: the state shown is the last one heard,
     *  which is worth saying without throwing it away. */
    stale?: boolean;
  }

  let { state, stale = false }: Props = $props();

  // `starting` and `stopping` share the in-transit look on purpose — what the
  // operator needs from across a room is "settled or moving", and the word
  // itself says which direction.
  const tone: Record<SessionState, string> = {
    idle: "text-[var(--ink-dim)] border-[var(--edge)]",
    starting: "text-c64-yellow border-c64-yellow/50",
    running: "text-c64-green border-c64-green/50",
    stopping: "text-c64-yellow border-c64-yellow/50",
    error: "text-c64-red border-c64-red/60",
  };

  const busy = $derived(state === "starting" || state === "stopping");
</script>

<span
  class="inline-flex items-center gap-2 rounded-full border px-3 py-1
         font-mono text-xs tracking-wide uppercase {tone[state]}"
  class:opacity-50={stale}
>
  <span
    class="size-2 rounded-full bg-current"
    class:animate-pulse={busy && !stale}
    aria-hidden="true"
  ></span>
  {state}
</span>
