<script lang="ts">
  import { onDestroy } from "svelte";

  import { api } from "$lib/api";

  interface Props {
    /** Which system's screen. Part of the stream URL, so switching systems
     *  swaps the `<img>` src and the host swaps which machine it watches. */
    system: string;
    /** Whether this host can show a picture at all — false on a machine with
     *  no VIC of its own, and on a host with the screen turned off. */
    available: boolean;
  }

  let { system, available }: Props = $props();

  // Off by default. The picture is the machine's own video stream and the host
  // only asks for it while somebody is watching, so opening this panel is the
  // act that starts a couple of megabytes a second moving — worth a tap rather
  // than something every idle console does.
  let watching = $state(false);
  let broken = $state("");

  // A cache-buster per start: `multipart/x-mixed-replace` is a normal response
  // to a browser's cache, and reusing the URL after a stop can serve the last
  // frame of the old stream forever instead of opening a new one.
  let epoch = $state(0);
  const src = $derived(
    `/api/screen/stream?system=${encodeURIComponent(system)}&t=${epoch}`,
  );

  function start(): void {
    broken = "";
    epoch += 1;
    watching = true;
  }

  function stop(): void {
    watching = false;
  }

  // Leaving the screen stops the stream: the `<img>` is what holds the
  // connection open, so removing it is what releases the machine.
  onDestroy(stop);

  async function snapshot(): Promise<void> {
    broken = "";
    try {
      await api.screen();
    } catch (e) {
      broken = e instanceof Error ? e.message : String(e);
    }
  }
</script>

<section class="panel min-w-0 p-4">
  <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
    <h3 class="text-sm font-semibold tracking-wide uppercase">Screen</h3>
    {#if available}
      <button
        class="min-h-9 rounded-md border border-[var(--edge)] px-2 text-xs
               text-[var(--ink-dim)] hover:text-[var(--ink)]
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
        onclick={() => (watching ? stop() : start())}
      >
        {watching ? "Stop" : "Watch"}
      </button>
    {/if}
  </header>

  {#if !available}
    <p class="text-sm text-[var(--ink-dim)]">
      This machine has no video stream of its own. The picture comes from the Ultimate 64's FPGA
      tapping its VIC directly — an Ultimate II+ is a cartridge in someone else's C64, and a
      TeensyROM+ has no video path at all.
    </p>
  {:else if watching}
    <!-- One `<img>` is the entire client: the host answers
         `multipart/x-mixed-replace`, so the browser swaps frames itself with
         no script, no socket and no decoder in this page. -->
    <img
      {src}
      alt="The Commodore's screen, live"
      onerror={() => {
        stop();
        broken = "The picture stopped. The show may have ended, or the machine stopped streaming.";
        void snapshot();
      }}
      class="w-full rounded-md border border-[var(--edge)] bg-black"
      style="aspect-ratio: 4 / 3; object-fit: fill; image-rendering: pixelated"
    />
    <p class="mt-2 text-xs text-[var(--ink-dim)]">
      What the VIC is actually painting, from the machine itself — not what c64cast believes it
      wrote.
    </p>
  {:else}
    <p class="text-sm text-[var(--ink-dim)]">
      Watch what the Commodore is painting, from the machine's own video stream. It runs only while
      this is open.
    </p>
  {/if}

  {#if broken}
    <p class="mt-2 text-sm text-c64-red">{broken}</p>
  {/if}
</section>
