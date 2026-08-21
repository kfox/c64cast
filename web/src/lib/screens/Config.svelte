<script lang="ts">
  import { onMount, untrack } from "svelte";

  import { api } from "$lib/api";
  import { fetchLibrary, launch, withToggledFavorite } from "$lib/actions";
  import Button from "$lib/components/Button.svelte";
  import ConfigForm from "$lib/components/ConfigForm.svelte";
  import ConfigList from "$lib/components/ConfigList.svelte";
  import TomlEditor from "$lib/components/TomlEditor.svelte";
  import type { Console } from "$lib/console.svelte";
  import { drafts } from "$lib/drafts.svelte";
  import { describeError } from "$lib/errorsLogic";
  import { DocIndex, documentation, forgetMedia, mediaOfKind } from "$lib/introspect";
  import type { Router } from "$lib/router.svelte";
  import type { ConfigDetail, ConfigEdit, ConfigIndex, LibraryState, MediaIndex } from "$lib/types";

  interface Props {
    host: Console;
    router: Router;
    /** Told when the file on screen is deleted, so the shell's shared
     *  selection (the tab-bar Start button) doesn't keep pointing at it. */
    onselect: (ref: string) => void;
  }

  let { host, router, onselect }: Props = $props();

  let index = $state<ConfigIndex | null>(null);
  let library = $state<LibraryState | null>(null);
  let docs = $state<DocIndex | null>(null);
  let detail = $state<ConfigDetail | null>(null);
  let media = $state<Record<string, MediaIndex>>({});
  let loading = $state(false);
  let problem = $state("");
  let view = $state<"form" | "text">("form");

  // Edits survive clicking away to another file — and, because the store is the
  // app's rather than this screen's, away to another *screen* and back. The
  // alternative — a "you have unsaved changes" dialog on every navigation —
  // asks the reader to defend an edit they may just be comparing against
  // something else.
  let draft = $state("");

  // A click through a long list starts several loads; only the newest one is
  // allowed to land, or the screen settles on whichever file the network
  // happened to answer last.
  let generation = 0;

  const selected = $derived(router.tail);
  const pending = $derived(drafts.fields(selected));
  const edited = $derived(drafts.refs);
  const dirty = $derived(
    (detail !== null && draft !== detail.text) || Object.keys(pending).length > 0,
  );

  /** True when the file on screen is the one the session is running, which is
   *  what makes "saved" and "in effect" two different things worth saying. */
  const isRunning = $derived(
    detail !== null &&
      host.session?.state === "running" &&
      host.session.config_path === detail.abs_path,
  );

  onMount(() => {
    void refreshIndex();
    void refreshLibrary();
    documentation()
      .then((d) => (docs = d))
      .catch((e: unknown) => (problem = describeError(e)));
  });

  // `untrack` so this reacts to the *ref* only. `load` reads the draft map,
  // which changes on every keystroke, and a dependency on it would refetch the
  // file being typed into.
  $effect(() => {
    const ref = selected;
    untrack(() => void load(ref));
  });

  // Whichever media kinds the loaded config's scenes actually browse — fetched
  // once each (mediaOfKind caches per kind, not per config) and merged into
  // `media`, which ConfigForm reads to build each `file =` field's datalist.
  $effect(() => {
    const form = detail?.form;
    const loadedDocs = docs;
    if (!form || !loadedDocs) return;
    const kinds = new Set(
      form.scenes.flatMap((sc) => loadedDocs.sceneType(sc.type)?.media_kinds ?? []),
    );
    untrack(() => {
      for (const kind of kinds) {
        if (kind in media) continue;
        mediaOfKind(kind)
          .then((idx) => (media = { ...media, [kind]: idx }))
          .catch((e: unknown) => (problem = describeError(e)));
      }
    });
  });

  async function refreshIndex(): Promise<void> {
    try {
      index = await api.configs();
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function refreshLibrary(): Promise<void> {
    try {
      library = await fetchLibrary();
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function toggleFavorite(ref: string, on: boolean): Promise<void> {
    try {
      library = await withToggledFavorite(library, ref, on);
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function createNew(): Promise<void> {
    const root = index?.roots.find((r) => !r.readonly);
    const path = window.prompt(
      "Path for the new configuration (inside a writable root):",
      root ? `${root.label}/new.toml` : "new.toml",
    );
    if (!path) return;
    try {
      await api.createConfig(path);
      await refreshIndex();
      router.go("config", path);
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function duplicate(): Promise<void> {
    if (!selected) return;
    const suggested = selected.replace(/(\.toml)?$/i, "-copy.toml");
    const path = window.prompt("Path for the duplicate:", suggested);
    if (!path) return;
    try {
      await api.createConfig(path, selected);
      await refreshIndex();
      router.go("config", path);
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function remove(): Promise<void> {
    if (!selected) return;
    if (!window.confirm(`Delete ${selected}? This cannot be undone.`)) return;
    try {
      await api.deleteConfig(selected);
      drafts.clear(selected);
      await refreshIndex();
      router.go("config");
      onselect("");
    } catch (e) {
      problem = describeError(e);
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
      draft = drafts.text(ref) ?? loaded.text;
    } catch (e) {
      if (token !== generation) return;
      detail = null;
      problem = describeError(e);
    } finally {
      if (token === generation) loading = false;
    }
  }

  function edit(text: string): void {
    draft = text;
    drafts.setText(selected, text === (detail?.text ?? "") ? null : text);
  }

  function stage(next: Record<string, ConfigEdit>): void {
    drafts.setFields(selected, next);
  }

  /** A save changes what the file *is*, so the form beside it is re-read
   *  rather than left describing the version that was there a moment ago —
   *  and both editors drop what they had staged for it, since the file now
   *  says it. */
  async function reread(): Promise<void> {
    drafts.clear(selected);
    await load(selected);
    await refreshIndex();
  }

  /** Sections of the last save that a reload will not pick up. Cleared when
   *  the selection changes, because it is a fact about one save of one file. */
  let heldBack = $state<string[]>([]);

  $effect(() => {
    selected;
    untrack(() => (heldBack = []));
  });

  /** Rebuild the running scenes from the file just saved. Only offered for
   *  the config that is actually running: a reload is the supervisor's, not
   *  this file's, and pointing it at a config it isn't running would be a
   *  button that lies. */
  async function reload(): Promise<void> {
    problem = "";
    try {
      await api.reload();
      heldBack = [];
    } catch (e) {
      problem = describeError(e);
    }
  }

  /** Stop and start the session on this config. What a reload cannot do: the
   *  connection, the audio threads and the control surfaces are built once,
   *  and the settings that configure them are read exactly then. */
  async function restart(): Promise<void> {
    problem = "";
    try {
      await api.switch(selected);
      heldBack = [];
    } catch (e) {
      problem = describeError(e);
    }
  }

  async function afterSave(held: string[]): Promise<void> {
    heldBack = held;
    await reread();
  }

  /** A file just landed on the host for a scene of `sceneType` — drop that
   *  type's media kind(s) from `mediaOfKind`'s cache and re-fetch them, so the
   *  new file shows up in every datalist without a page reload. `media` is
   *  replaced rather than mutated, which is what makes ConfigForm's own
   *  per-scene-type cache (keyed off this same object) rebuild. */
  async function handleUploaded(sceneType: string): Promise<void> {
    const kinds = docs?.sceneType(sceneType)?.media_kinds ?? [];
    for (const kind of kinds) {
      forgetMedia(kind);
      try {
        media = { ...media, [kind]: await mediaOfKind(kind) };
      } catch (e) {
        problem = describeError(e);
      }
    }
  }

  const clock = (t: number) => new Date(t * 1000).toLocaleString();

  let launching = $state(false);

  async function startSelected(ref: string = selected): Promise<void> {
    if (!ref) return;
    launching = true;
    problem = "";
    try {
      await launch(host, ref);
    } catch (e) {
      problem = describeError(e);
    } finally {
      launching = false;
    }
  }
</script>

<div class="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
  <section class="panel min-w-0 p-5">
    <header class="mb-3 flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Editor</h2>
      <Button onclick={refreshIndex}>Refresh</Button>
    </header>
    {#if !host.readOnly}
      <div class="mb-3 flex flex-wrap gap-2">
        <Button onclick={createNew}>New</Button>
        <Button disabled={!selected} onclick={duplicate}>Duplicate</Button>
        <Button variant="danger" disabled={!selected} onclick={remove}>Delete</Button>
      </div>
    {/if}
    {#if index === null}
      <p class="text-sm text-[var(--ink-dim)]">Loading…</p>
    {:else}
      <ConfigList
        {index}
        {edited}
        tall
        value={selected}
        onselect={(ref: string) => router.go("config", ref)}
        onstart={(ref: string) => {
          router.go("config", ref);
          void startSelected(ref);
        }}
        favorites={library?.favorites ?? []}
        onfavorite={toggleFavorite}
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
      <header class="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="font-mono text-lg font-semibold break-all">{detail.path}</h2>
          <p class="mt-1 text-xs text-[var(--ink-dim)]">
            {detail.size} bytes · {clock(detail.mtime)}
            {#if detail.kind === "ensemble"}
              · ensemble master over {detail.systems.join(", ")}
            {/if}
          </p>
        </div>
        {#if !host.readOnly && !isRunning}
          <Button
            variant="primary"
            disabled={launching || host.busy}
            onclick={() => startSelected()}
          >
            {host.session?.state === "running" ? "Switch to this" : "Start"}
          </Button>
        {/if}
      </header>

      {#if isRunning}
        <div class="mb-4 space-y-2 rounded-lg bg-[var(--panel-alt)] px-3 py-2">
          <div class="flex flex-wrap items-center gap-3">
            <p class="flex-1 text-sm">
              This is what the session is running. A save lands on disk; the show picks it up on a
              reload.
            </p>
            {#if !host.readOnly}
              <Button onclick={reload}>Reload scenes</Button>
            {/if}
          </div>
          {#if heldBack.length}
            <!-- A reload re-reads the file and rebuilds the scenes; it does not
                 rebuild the connection or restart the audio threads. Offering
                 it alone here would be a button that quietly does nothing for
                 what was just changed. -->
            <div class="flex flex-wrap items-center gap-3 border-t border-[var(--edge)] pt-2">
              <p class="flex-1 text-sm text-c64-yellow">
                A reload will not pick up {heldBack.map((s) => `[${s}]`).join(", ")} — those are
                read once, when the session starts.
              </p>
              {#if !host.readOnly}
                <Button variant="primary" onclick={restart}>Restart on this config</Button>
              {/if}
            </div>
          {/if}
        </div>
      {/if}

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
        <ConfigForm
          form={detail.form}
          {docs}
          path={detail.path}
          readOnly={host.readOnly}
          {pending}
          {media}
          onpending={stage}
          onsaved={(_written, held) => void afterSave(held)}
          onuploaded={(sceneType) => void handleUploaded(sceneType)}
        />
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
