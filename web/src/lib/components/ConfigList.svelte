<script lang="ts">
  import { displayLabel, type ConfigSort, visibleRows } from "$lib/configListLogic";
  import type { ConfigIndex } from "$lib/types";

  interface Props {
    index: ConfigIndex | null;
    /** The selected ref, or "" for none. Owned by the caller rather than
     *  bound, because one caller keeps it in a variable and the other keeps
     *  it in the address bar. */
    value: string;
    onselect: (ref: string) => void;
    /** Double-clicking a row starts it. Omitted on a screen (the editor) where
     *  a double-click should just select, not launch a show. */
    onstart?: (ref: string) => void;
    /** Refs marked as favorites, and the toggle for one. Omitted where there
     *  is no room for the star (a small embedded picker). */
    favorites?: readonly string[];
    onfavorite?: (ref: string, on: boolean) => void;
    /** Refs with unsaved edits, marked so a half-written config is not lost
     *  behind a click on something else. */
    edited?: readonly string[];
    /** Give the list the height it wants. On the session screen it is one
     *  panel among several and stays boxed; on the editor it is the navigation
     *  for everything to its right. */
    tall?: boolean;
  }

  let {
    index,
    value,
    onselect,
    onstart,
    favorites = [],
    onfavorite,
    edited = [],
    tall = false,
  }: Props = $props();

  let query = $state("");
  let sort = $state<ConfigSort>("name-asc");
  // Off by default: the list this control replaced was every file under every
  // root, and the packaged examples are the part of that a working show
  // rarely wants mixed back in with it.
  let showExamples = $state(false);

  const SORTS: { id: ConfigSort; label: string }[] = [
    { id: "name-asc", label: "Name A–Z" },
    { id: "name-desc", label: "Name Z–A" },
    { id: "newest", label: "Newest" },
    { id: "oldest", label: "Oldest" },
  ];

  const label = displayLabel;
  const rows = $derived(visibleRows(index?.files ?? [], { query, showExamples, sort }));
  const favoriteSet = $derived(new Set(favorites));

  function select(ref: string): void {
    onselect(ref);
  }

  function start(ref: string): void {
    (onstart ?? onselect)(ref);
  }

  function toggleFavorite(ref: string, e: MouseEvent): void {
    // The star sits inside the row's own button; without this the click also
    // selects (or, worse, starts) the row it was meant to just mark.
    e.stopPropagation();
    onfavorite?.(ref, !favoriteSet.has(ref));
  }
</script>

<div class="mb-2 flex flex-wrap items-center gap-2">
  <input
    type="search"
    placeholder="Search…"
    aria-label="Search configurations"
    bind:value={query}
    class="min-h-9 min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-[var(--panel-alt)]
           px-2 text-sm focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
  />
  <select
    aria-label="Sort configurations"
    bind:value={sort}
    class="min-h-9 rounded-md border border-[var(--edge)] bg-[var(--panel-alt)] px-1.5 text-sm"
  >
    {#each SORTS as s (s.id)}
      <option value={s.id}>{s.label}</option>
    {/each}
  </select>
  <label class="flex min-h-9 items-center gap-1.5 text-xs text-[var(--ink-dim)]">
    <input type="checkbox" bind:checked={showExamples} class="size-4" />
    Examples
  </label>
</div>

<ul
  class="space-y-1 overflow-y-auto {tall ? 'max-h-[70vh]' : 'max-h-72'}"
  role="listbox"
  aria-label="Configurations"
>
  {#each rows as row (row.path)}
    <li>
      <!-- A `div`, not a `button`: the favorite star beside the label is a
           real button of its own, and a button cannot nest one. -->
      <div
        role="option"
        tabindex="0"
        aria-selected={value === row.path}
        onclick={() => select(row.path)}
        ondblclick={() => start(row.path)}
        onkeydown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            select(row.path);
          }
        }}
        class="flex w-full cursor-pointer items-start gap-1.5 rounded-md border px-2.5 py-2 text-sm
               focus-visible:outline-2 focus-visible:outline-[var(--accent)]
               {value === row.path
          ? 'border-[var(--accent)] bg-[var(--panel-alt)]'
          : 'border-transparent hover:bg-[var(--panel-alt)]'}"
      >
        <span class="min-w-0 flex-1">
          <span class="block font-mono break-all">
            {label(row)}
            {#if row.readonly}
              <span class="text-[var(--ink-dim)]">(example)</span>
            {/if}
          </span>
          {#if edited.includes(row.path)}
            <span class="text-c64-yellow" title="Unsaved changes">• unsaved</span>
          {/if}
        </span>
        {#if onfavorite}
          <button
            type="button"
            aria-pressed={favoriteSet.has(row.path)}
            aria-label={favoriteSet.has(row.path) ? "Remove favorite" : "Add favorite"}
            onclick={(e) => toggleFavorite(row.path, e)}
            class="min-h-7 min-w-7 shrink-0 rounded text-base leading-none
                   focus-visible:outline-2 focus-visible:outline-[var(--accent)]
                   {favoriteSet.has(row.path) ? 'text-c64-yellow' : 'text-[var(--ink-dim)]'}"
          >
            {favoriteSet.has(row.path) ? "★" : "☆"}
          </button>
        {/if}
      </div>
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
{:else if index && rows.length === 0}
  <p class="mt-3 text-xs text-[var(--ink-dim)]">
    {index.files.length === 0 ? "No configurations found." : "Nothing matches."}
  </p>
{/if}
