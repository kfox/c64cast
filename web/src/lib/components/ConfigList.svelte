<script lang="ts">
  import type { ConfigIndex } from "$lib/types";

  interface Props {
    index: ConfigIndex | null;
    /** The selected ref, or "" for whatever the host was launched with. Owned
     *  by the caller rather than bound, because one caller keeps it in a
     *  variable and the other keeps it in the address bar. */
    value: string;
    onselect: (ref: string) => void;
    /** Offer the host's own configuration as a row. The session screen starts
     *  shows and wants it; the editor edits files and cannot open a config the
     *  host was handed on the command line. */
    hostDefault?: boolean;
    /** Refs with unsaved edits, marked so a half-written config is not lost
     *  behind a click on something else. */
    edited?: readonly string[];
    /** Give the list the height it wants. On the session screen it is one
     *  panel among several and stays boxed; on the editor it is the navigation
     *  for everything to its right. */
    tall?: boolean;
  }

  let { index, value, onselect, hostDefault = true, edited = [], tall = false }: Props = $props();

  // The list and the chooser are one control on purpose. Two — a dropdown to
  // act on plus a list to look at — is what this screen had first, and every
  // reading of it had to reconcile which one was authoritative.
  const rows = $derived([
    ...(hostDefault
      ? [{ ref: "", label: "Host default", hint: "the configuration --serve was launched with" }]
      : []),
    ...(index?.files ?? []).map((f) => ({ ref: f.path, label: f.path, hint: "" })),
  ]);
</script>

<ul
  class="space-y-1 overflow-y-auto {tall ? 'max-h-[70vh]' : 'max-h-72'}"
  role="listbox"
  aria-label="Configurations"
>
  {#each rows as row (row.ref)}
    <li>
      <button
        role="option"
        aria-selected={value === row.ref}
        onclick={() => onselect(row.ref)}
        class="w-full rounded-md border px-2.5 py-2 text-left text-sm break-all
               {value === row.ref
          ? 'border-[var(--accent)] bg-[var(--panel-alt)]'
          : 'border-transparent hover:bg-[var(--panel-alt)]'}"
      >
        <span class:font-mono={row.ref !== ""}>{row.label}</span>
        {#if edited.includes(row.ref)}
          <span class="ml-1 text-c64-yellow" title="Unsaved changes">•</span>
        {/if}
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
