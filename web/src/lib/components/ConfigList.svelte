<script lang="ts">
  import type { ConfigIndex } from "$lib/types";

  interface Props {
    index: ConfigIndex | null;
    /** The ref to run, or "" for whatever the host was launched with. */
    value: string;
  }

  let { index, value = $bindable() }: Props = $props();

  // The list and the chooser are one control on purpose. Two — a dropdown to
  // act on plus a list to look at — is what this screen had first, and every
  // reading of it had to reconcile which one was authoritative.
  const rows = $derived([
    { ref: "", label: "Host default", hint: "the configuration --serve was launched with" },
    ...(index?.files ?? []).map((f) => ({ ref: f.path, label: f.path, hint: "" })),
  ]);
</script>

<ul class="max-h-72 space-y-1 overflow-y-auto" role="listbox" aria-label="Configurations">
  {#each rows as row (row.ref)}
    <li>
      <button
        role="option"
        aria-selected={value === row.ref}
        onclick={() => (value = row.ref)}
        class="w-full rounded-md border px-2.5 py-2 text-left text-sm break-all
               {value === row.ref
          ? 'border-[var(--accent)] bg-[var(--panel-alt)]'
          : 'border-transparent hover:bg-[var(--panel-alt)]'}"
      >
        <span class:font-mono={row.ref !== ""}>{row.label}</span>
        {#if row.hint}
          <span class="block text-xs text-[var(--ink-dim)]">{row.hint}</span>
        {/if}
      </button>
    </li>
  {/each}
</ul>

{#if index && index.roots.length === 0}
  <p class="mt-3 text-xs text-[var(--ink-dim)]">
    No config roots are set. Point <code class="font-mono">[web].config_roots</code> at the directory
    your playlists live in to list and start them by name.
  </p>
{:else if index?.truncated}
  <p class="mt-3 text-xs text-c64-yellow">
    Truncated — there are more configurations under these roots than the browser will list.
  </p>
{/if}
