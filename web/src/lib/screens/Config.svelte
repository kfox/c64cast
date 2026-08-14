<script lang="ts">
  import { onMount, untrack } from "svelte";

  import { ApiError, api } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import ConfigForm from "$lib/components/ConfigForm.svelte";
  import ConfigList from "$lib/components/ConfigList.svelte";
  import TomlEditor from "$lib/components/TomlEditor.svelte";
  import type { Console } from "$lib/console.svelte";
  import { DocIndex, documentation } from "$lib/introspect";
  import type { Router } from "$lib/router.svelte";
  import type { ConfigDetail, ConfigIndex } from "$lib/types";

  interface Props {
    host: Console;
    router: Router;
  }

  let { host, router }: Props = $props();

  let index = $state<ConfigIndex | null>(null);
  let docs = $state<DocIndex | null>(null);
  let detail = $state<ConfigDetail | null>(null);
  let loading = $state(false);
  let problem = $state("");
  let view = $state<"form" | "text">("form");
  let onlyChanged = $state(true);

  // Edits survive clicking away to another file and back. The alternative —
  // a "you have unsaved changes" dialog on every navigation — asks the reader
  // to defend an edit they may just be comparing against something else.
  let drafts = $state<Record<string, string>>({});
  let draft = $state("");

  // A click through a long list starts several loads; only the newest one is
  // allowed to land, or the screen settles on whichever file the network
  // happened to answer last.
  let generation = 0;

  const selected = $derived(router.tail);
  const edited = $derived(Object.keys(drafts));
  const dirty = $derived(detail !== null && draft !== detail.text);

  onMount(() => {
    void refreshIndex();
    documentation()
      .then((d) => (docs = d))
      .catch((e: unknown) => (problem = describe(e)));
  });

  // `untrack` so this reacts to the *ref* only. `load` reads the draft map,
  // which changes on every keystroke, and a dependency on it would refetch the
  // file being typed into.
  $effect(() => {
    const ref = selected;
    untrack(() => void load(ref));
  });

  async function refreshIndex(): Promise<void> {
    try {
      index = await api.configs();
    } catch (e) {
      problem = describe(e);
    }
  }

  async function load(ref: string): Promise<void> {
    const token = ++generation;
    if (!ref) {
      detail = null;
      draft = "";
      return;
    }
    loading = true;
    problem = "";
    try {
      const loaded = await api.config(ref);
      if (token !== generation) return;
      detail = loaded;
      draft = drafts[ref] ?? loaded.text;
    } catch (e) {
      if (token !== generation) return;
      detail = null;
      problem = describe(e);
    } finally {
      if (token === generation) loading = false;
    }
  }

  function describe(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.status === 403) return `Not allowed: ${e.message}`;
      if (e.status === 404) return `No such configuration: ${e.message}`;
      if (e.status === 413) return `That file is too large to edit here: ${e.message}`;
      return e.message;
    }
    return e instanceof Error ? e.message : String(e);
  }

  function edit(text: string): void {
    draft = text;
    drafts = withDraft(text === (detail?.text ?? "") ? null : text);
  }

  function withDraft(text: string | null): Record<string, string> {
    const next = { ...drafts };
    if (text === null) delete next[selected];
    else next[selected] = text;
    return next;
  }

  /** A save changes what the file *is*, so the form beside it is re-read
   *  rather than left describing the version that was there a moment ago. */
  async function reread(): Promise<void> {
    drafts = withDraft(null);
    await load(selected);
    await refreshIndex();
  }

  const clock = (t: number) => new Date(t * 1000).toLocaleString();
</script>

<div class="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
  <section class="panel p-5">
    <header class="mb-3 flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Configurations</h2>
      <Button onclick={refreshIndex}>Refresh</Button>
    </header>
    {#if index === null}
      <p class="text-sm text-[var(--ink-dim)]">Loading…</p>
    {:else}
      <ConfigList
        {index}
        {edited}
        tall
        hostDefault={false}
        value={selected}
        onselect={(ref: string) => router.go("config", ref)}
      />
    {/if}
  </section>

  <section class="panel min-w-0 p-5">
    {#if problem}
      <p class="mb-4 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
        {problem}
      </p>
    {/if}

    {#if !selected}
      <h2 class="text-lg font-semibold">No configuration selected</h2>
      <p class="mt-2 text-sm text-[var(--ink-dim)]">
        Pick a file to read what it sets and what it leaves alone, edit it, and save it back. The
        Session screen is where one gets started.
      </p>
    {:else if loading && detail === null}
      <p class="text-sm text-[var(--ink-dim)]">Loading {selected}…</p>
    {:else if detail}
      <header class="mb-4">
        <h2 class="font-mono text-lg font-semibold break-all">{detail.path}</h2>
        <p class="mt-1 text-xs text-[var(--ink-dim)]">
          {detail.size} bytes · {clock(detail.mtime)}
          {#if detail.kind === "ensemble"}
            · ensemble master over {detail.systems.join(", ")}
          {/if}
        </p>
      </header>

      {#if detail.error}
        <p class="mb-4 rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
          This file does not load: {detail.error}
        </p>
      {/if}

      {#if detail.unknown_keys.length}
        <ul class="mb-4 rounded-lg border border-c64-yellow/50 px-3 py-2 text-sm text-c64-yellow">
          {#each detail.unknown_keys as key, i (i)}
            <li>
              <span class="font-mono">[{key.section}] {key.key}</span>
              {key.hint ? `— ${key.hint}` : "is not a key c64cast knows."}
            </li>
          {/each}
        </ul>
      {/if}

      <div class="mb-4 flex flex-wrap items-center gap-2">
        <div class="flex gap-1 rounded-lg border border-[var(--edge)] p-1" role="tablist">
          {#each [["form", "Settings"], ["text", "Source"]] as [id, label] (id)}
            <button
              role="tab"
              aria-selected={view === id}
              onclick={() => (view = id as "form" | "text")}
              class="min-h-9 rounded-md px-3 text-sm
                     {view === id ? 'bg-[var(--accent)] text-[var(--accent-ink)]' : ''}"
            >
              {label}
            </button>
          {/each}
        </div>
        {#if dirty}
          <span class="text-xs text-c64-yellow">edited, not saved</span>
        {/if}
      </div>

      {#if view === "text"}
        <TomlEditor
          path={detail.path}
          value={draft}
          baseline={detail.text}
          readOnly={host.readOnly}
          onchange={edit}
          onsaved={() => void reread()}
        />
      {:else if detail.form && docs}
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={onlyChanged} class="size-4" />
            Only what this file changes
          </label>
          <p class="text-xs text-[var(--ink-dim)]">
            Values are what the loader resolved, so machine settings and defaults show through.
          </p>
        </div>
        <ConfigForm form={detail.form} {docs} {onlyChanged} />
      {:else if detail.kind === "ensemble"}
        <p class="text-sm text-[var(--ink-dim)]">
          An ensemble master is authored across several files, so there is no single set of settings
          to lay out. Edit it as source.
        </p>
      {:else if detail.error}
        <p class="text-sm text-[var(--ink-dim)]">
          Settings cannot be shown for a file that does not load. Fix it as source.
        </p>
      {:else}
        <p class="text-sm text-[var(--ink-dim)]">Loading the field documentation…</p>
      {/if}
    {/if}
  </section>
</div>
