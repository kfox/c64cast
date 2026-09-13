<script lang="ts">
  import { ApiError, api, reportOf } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import Diagnostics from "$lib/components/Diagnostics.svelte";
  import FieldRow from "$lib/components/FieldRow.svelte";
  import LayerBlame from "$lib/components/LayerBlame.svelte";
  import MediaWarnings from "$lib/components/MediaWarnings.svelte";
  import { debounce } from "$lib/debounce";
  import { searchMedia, type DocIndex } from "$lib/introspect";
  import { kindsForScene, pickerOptions, uploadMessage, urlFromDrop } from "$lib/mediaPickerLogic";
  import { uploadLabel } from "$lib/uploadLogic";
  import type {
    ConfigEdit,
    ConfigForm,
    ConfigWritten,
    FormField,
    FormScene,
    FormSection,
    MediaIndex,
    SceneTypeDoc,
    ValidationReport,
    Warning,
  } from "$lib/types";

  interface Props {
    form: ConfigForm;
    docs: DocIndex;
    /** The config ref this form belongs to — what the save PATCHes. */
    path: string;
    readOnly: boolean;
    /** Edits typed but not saved, held by the screen. Keyed by row. */
    pending: Record<string, ConfigEdit>;
    onpending: (next: Record<string, ConfigEdit>) => void;
    /** `restart` names the sections a reload will *not* pick up. Empty on a
     *  save a reload covers in full, which includes every structural change. */
    onsaved: (written: ConfigWritten, restart: string[]) => void;
    /** Media kind -> what's browsable there (`Config.svelte` fetches one
     *  listing per kind any loaded scene type uses). An absent kind renders an
     *  empty datalist, which is a plain text box. */
    media?: Record<string, MediaIndex>;
    /** A file just landed on the host for this scene type — `Config.svelte`
     *  drops and re-fetches its cached listing for the kind(s) that type
     *  browses. */
    onuploaded?: (sceneType: string) => void;
  }

  let {
    form,
    docs,
    path,
    readOnly,
    pending,
    onpending,
    onsaved,
    media = {},
    onuploaded,
  }: Props = $props();

  /** The datalist options a scene type's `file =` field offers: the union of
   *  every media kind it browses, deduplicated. Cached by scene index — not by
   *  scene type name, so two scenes sharing a type never share the other's
   *  datalist. A kind with a live search overlays `media`'s unfiltered listing
   *  entirely rather than merging with it. */
  const mediaOptions = $derived.by(() => {
    const cache = new Map<number, string[]>();
    return (doc: SceneTypeDoc | undefined, sceneIndex: number): string[] => {
      let options = cache.get(sceneIndex);
      if (!options) {
        const entries = kindsForScene(doc).flatMap(
          (kind) => searchResults[searchKey(sceneIndex, kind)]?.entries ?? media[kind]?.entries ?? [],
        );
        options = [...new Set(pickerOptions(entries))];
        cache.set(sceneIndex, options);
      }
      return options;
    };
  });

  /** Whether any kind this scene type browses has stopped short of every
   *  match on the host — a live search past `MAX_FILES`, or (with no search
   *  live for that scene+kind) the unfiltered listing itself. */
  function mediaTruncated(doc: SceneTypeDoc | undefined, sceneIndex: number): boolean {
    return kindsForScene(doc).some(
      (kind) => (searchResults[searchKey(sceneIndex, kind)] ?? media[kind])?.truncated === true,
    );
  }

  const SEARCH_DEBOUNCE_MS = 250;

  // A live search's result per scene index + kind. Keyed by scene index as
  // well as kind, so searching in one scene never changes the datalist of
  // another that shares its type. `null` means no query is live for that
  // scene+kind, so `mediaOptions` falls back to the unfiltered `media` prop.
  let searchResults = $state<Record<string, MediaIndex | null>>({});

  function searchKey(sceneIndex: number, kind: string): string {
    return `${sceneIndex}:${kind}`;
  }

  // One debounced fetch per scene+kind, so a burst within one field coalesces
  // without a keystroke in a *different* field clobbering it.
  // `searchGenerations` pairs with it: each firing bumps its key's generation,
  // and a fetch resolving against a superseded generation is dropped, so a
  // slow answer to a broad query cannot overwrite a fast answer to a narrower
  // one already on screen.
  const searchDebouncers = new Map<string, (q: string) => void>();
  const searchGenerations = new Map<string, number>();

  function debouncedSearch(key: string, kind: string): (q: string) => void {
    let debounced = searchDebouncers.get(key);
    if (!debounced) {
      debounced = debounce((q: string) => {
        const generation = (searchGenerations.get(key) ?? 0) + 1;
        searchGenerations.set(key, generation);
        searchMedia(kind, q)
          .then((idx) => {
            if (searchGenerations.get(key) !== generation) return;
            searchResults = { ...searchResults, [key]: idx };
          })
          .catch(() => {
            // Leave whatever was showing rather than blank the datalist.
          });
      }, SEARCH_DEBOUNCE_MS);
      searchDebouncers.set(key, debounced);
    }
    return debounced;
  }

  /** Search every kind scene `sceneIndex` browses — a `file =` field's
   *  datalist is their union, so its search is too. An empty query clears
   *  immediately rather than waiting out the debounce, and bumps the
   *  generation so a fetch already in flight cannot land afterward. */
  function searchScene(doc: SceneTypeDoc | undefined, sceneIndex: number, q: string): void {
    for (const kind of kindsForScene(doc)) {
      const key = searchKey(sceneIndex, kind);
      if (!q.trim()) {
        searchGenerations.set(key, (searchGenerations.get(key) ?? 0) + 1);
        searchResults = { ...searchResults, [key]: null };
      } else {
        debouncedSearch(key, kind)(q);
      }
    }
  }

  /** Hide every field still sitting at its baseline. On by default: a config
   *  has far more settable fields than a show file names. */
  let onlyChanged = $state(true);
  let query = $state("");
  let report = $state<ValidationReport | null>(null);
  let problem = $state("");
  let saved = $state("");
  let busy = $state(false);
  // `uploadTotal` starts at the file's own size, so the bar has a real number
  // before the first `progress` event lands.
  let uploadingIndex = $state<number | null>(null);
  let uploadingName = $state("");
  let uploadLoaded = $state(0);
  let uploadTotal = $state(0);
  let uploadComputable = $state(true);
  let uploadAbort = $state<AbortController | null>(null);
  let uploadNote = $state("");

  // Half-typed values, kept here rather than beside the edits: a number that
  // isn't one yet is not carried to another file and back.
  let invalid = $state<Record<string, string>>({});
  let warnings = $state<Warning[]>([]);

  const edits = $derived(Object.values(pending));
  const blocked = $derived(Object.values(invalid).some(Boolean));

  /** The sections among the staged edits that a reload will not pick up.
   *
   * A reload re-reads the file and hands the playlist fresh scenes, so a scene
   * edit lands; the connection, the audio threads and the control surfaces are
   * built once with the session and do not. Which sections are which is the
   * host's answer (`SectionDoc.reload`), not a list kept here. */
  const restartEdits = $derived(
    edits.filter((edit) => !!edit.section && !docs.section(edit.section)?.reload),
  );
  const restart = $derived([...new Set(restartEdits.map((edit) => edit.section as string))]);

  /** A row's identity — one string, naming a section *or* a scene index the
   *  way the wire shape does. */
  const sectionKey = (section: string, field: string) => `s:${section}.${field}`;
  const sceneKey = (index: number, field: string) => `n:${index}.${field}`;

  const needle = $derived(query.trim().toLowerCase());

  /** Whether *any* field is named like the query. Names are searched first and
   *  alone — matching help text on "color" pulls in everything that mentions
   *  color — and a query that names nothing falls through to the descriptions,
   *  which the form says it has done. */
  const byName = $derived(
    needle !== "" &&
      [
        ...form.sections.flatMap((s) => s.fields.map((f) => f.name)),
        ...form.scenes.flatMap((s) => s.fields.map((f) => f.name)),
      ].some((name) => name.toLowerCase().includes(needle)),
  );

  function shown(fields: FormField[], key: (f: FormField) => string, help: HelpOf): FormField[] {
    return fields.filter((f) => {
      // An unsaved edit is never hidden by a filter.
      if (pending[key(f)]) return true;
      // A search outranks the "only what this file changes" filter.
      if (needle) return matches(f.name) || (!byName && help(f.name).toLowerCase().includes(needle));
      return !onlyChanged || !f.is_default;
    });
  }

  type HelpOf = (field: string) => string;

  function matches(name: string): boolean {
    return name.toLowerCase().includes(needle);
  }

  const sections = $derived(
    form.sections
      .map((s: FormSection) => ({
        section: s,
        fields: shown(
          s.fields,
          (f) => sectionKey(s.name, f.name),
          (name) => docs.field(s.name, name)?.help ?? "",
        ),
      }))
      .filter((row) => row.fields.length > 0),
  );

  // Scenes always show: one with every field at its default is still a scene
  // the playlist will run.
  const scenes = $derived(
    form.scenes.map((sc: FormScene, i: number) => ({
      scene: sc,
      index: i,
      fields: shown(
        sc.fields,
        (f) => sceneKey(i, f.name),
        (name) => docs.sceneField(sc.type, name)?.help ?? "",
      ),
    })),
  );

  function overlayEntries(overlay: unknown): [string, unknown][] {
    if (!overlay || typeof overlay !== "object") return [];
    return Object.entries(overlay as Record<string, unknown>).filter(([k]) => k !== "type");
  }

  function overlayType(overlay: unknown): string {
    const type = (overlay as { type?: unknown } | null)?.type;
    return typeof type === "string" ? type : "overlay";
  }

  /** What the row shows: the edit if there is one, else what is on disk. A
   *  cleared row shows what it will fall back to. */
  function shownValue(field: FormField, key: string): unknown {
    const edit = pending[key];
    if (!edit) return field.value;
    return edit.reset ? field.baseline : edit.value;
  }

  function stage(key: string, edit: ConfigEdit, field: FormField, value: unknown, error: string): void {
    invalid = { ...invalid, [key]: error };
    if (error) return;
    // Typing the stored value back is not an edit. JSON-compared, since a list
    // or a table is a value here like any other.
    const same = JSON.stringify(value) === JSON.stringify(field.value);
    onpending(same ? without(key) : { ...pending, [key]: { ...edit, value } });
  }

  /** Stop setting the field here. On a row the file never set, that is the
   *  same as dropping the edit. */
  function clear(key: string, edit: ConfigEdit, field: FormField): void {
    invalid = { ...invalid, [key]: "" };
    onpending(field.is_default ? without(key) : { ...pending, [key]: { ...edit, reset: true } });
  }

  function revert(key: string): void {
    invalid = { ...invalid, [key]: "" };
    onpending(without(key));
  }

  function without(key: string): Record<string, ConfigEdit> {
    const next = { ...pending };
    delete next[key];
    return next;
  }

  function discard(): void {
    invalid = {};
    report = null;
    problem = "";
    saved = "";
    warnings = [];
    onpending({});
  }

  /** What it takes to *see* the change that was just saved. */
  function applies(count: number, held: number, sections: string[]): string {
    const named = sections.map((s) => `[${s}]`).join(", ");
    const verb = sections.length === 1 ? "needs" : "need";
    if (held === 0) return count === 1 ? "It applies on a reload." : "They apply on a reload.";
    if (held === count) return `${named} ${verb} the session restarted.`;
    return `${named} ${verb} the session restarted; the rest apply on a reload.`;
  }

  /** Which scene type a new blank scene gets; the options are the host's
   *  list. */
  let newType = $state("video");

  const chip = `min-h-9 rounded-md border border-[var(--edge)] px-2 text-xs
                text-[var(--ink-dim)] hover:text-[var(--ink)] disabled:opacity-40`;

  /** Adding or removing a scene renumbers the ones after it, and every staged
   *  edit is keyed by index — so the two cannot be in flight at once. Refusing
   *  is better than renumbering the staged edits, which would silently move an
   *  unsaved change onto a different scene. */
  const structuralBlocked = $derived(edits.length > 0);

  /** The ↑/↓ chips for a scene at `index`, built from one shape so the
   *  earlier/later pair cannot drift apart. */
  function moveDirections(index: number, sceneCount: number) {
    return [
      { delta: -1, symbol: "↑", label: "Move this scene earlier", atEdge: index === 0 },
      { delta: 1, symbol: "↓", label: "Move this scene later", atEdge: index === sceneCount - 1 },
    ];
  }

  async function structural(act: () => Promise<ConfigWritten>): Promise<void> {
    report = null;
    problem = "";
    saved = "";
    warnings = [];
    busy = true;
    try {
      const written = await act();
      const note = uploadNote ? `${uploadNote} ` : "";
      saved = `${note}Saved. ${written.backup ? `The previous version is in ${written.backup}.` : ""}`;
      warnings = written.warnings ?? [];
      onsaved(written, []);
    } catch (e) {
      const refused = reportOf(e);
      const note = uploadNote ? `${uploadNote} ` : "";
      if (refused) report = refused;
      else if (e instanceof ApiError) problem = `${note}${e.message}`;
      else problem = `${note}${e instanceof Error ? e.message : String(e)}`;
    } finally {
      busy = false;
      uploadNote = "";
    }
  }

  /** Upload a file dropped or picked for a scene's `fieldName`, then PATCH
   *  that field to the spec the upload landed at — through `structural()`, so
   *  the busy flag, error handling and post-save re-read are a scene
   *  add/remove's. */
  async function uploadFile(index: number, fieldName: string, file: File): Promise<void> {
    if (readOnly || busy || structuralBlocked) return;
    uploadingIndex = index;
    uploadingName = file.name;
    uploadLoaded = 0;
    uploadTotal = file.size;
    uploadComputable = true;
    uploadAbort = new AbortController();
    try {
      await structural(async () => {
        const uploaded = await api.uploadMedia(file.name, file, {
          onProgress: (loaded, total, computable) => {
            uploadLoaded = loaded;
            uploadTotal = total;
            uploadComputable = computable;
          },
          signal: uploadAbort?.signal,
        });
        // The XHR is in state DONE, so abort() would be a no-op from here.
        uploadAbort = null;
        uploadNote = uploadMessage(uploaded);
        onuploaded?.(form.scenes[index]?.type ?? "");
        return api.patchConfig(path, [{ scene: index, field: fieldName, value: uploaded.spec }]);
      });
    } finally {
      uploadingIndex = null;
      uploadingName = "";
      uploadAbort = null;
    }
  }

  /** Abort the upload in flight, if there is one. Its rejection reaches
   *  `structural`'s own catch like any other failure. */
  function cancelUpload(): void {
    uploadAbort?.abort();
  }

  let dragOverIndex = $state<number | null>(null);

  /** Dropping a **file** onto a scene uploads it (see `uploadFile`); dropping
   *  a **URL** instead sets its `file =` field directly, with no upload
   *  involved. Files are checked first — a Finder or Explorer drag carries
   *  both a `File` and a `file:///` `text/uri-list` entry, and the latter is
   *  not a URL this console should fetch. */
  async function dropUrl(index: number, event: DragEvent): Promise<void> {
    event.preventDefault();
    dragOverIndex = null;
    if (readOnly || busy || structuralBlocked) return;
    const dt = event.dataTransfer;
    const file = dt?.files?.[0];
    if (file) {
      await uploadFile(index, "file", file);
      return;
    }
    const url = urlFromDrop({
      "text/uri-list": dt?.getData("text/uri-list") ?? "",
      "text/plain": dt?.getData("text/plain") ?? "",
    });
    if (!url) {
      problem = "Drop a file or a URL to set a scene's file.";
      return;
    }
    await structural(() => api.patchConfig(path, [{ scene: index, field: "file", value: url }]));
  }

  async function save(): Promise<void> {
    report = null;
    problem = "";
    saved = "";
    warnings = [];
    busy = true;
    // Read before the save: `onsaved` re-reads the file, clearing the staged
    // edits these are derived from.
    const needsRestart = restart;
    const count = edits.length;
    const held = restartEdits.length;
    try {
      const written = await api.patchConfig(path, edits);
      const what = count === 1 ? "1 change" : `${count} changes`;
      const kept = written.backup ? ` The previous version is in ${written.backup}.` : "";
      saved = `Saved ${what}. ${applies(count, held, needsRestart)}${kept}`;
      warnings = written.warnings ?? [];
      invalid = {};
      onsaved(written, needsRestart);
    } catch (e) {
      // A refused save answers 422 with the whole validation report — the same
      // shape the text editor's Check returns. The edits stay staged; the file
      // is untouched.
      const refused = reportOf(e);
      if (refused) report = refused;
      else if (e instanceof ApiError) problem = e.message;
      else problem = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<div class="space-y-6">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <input
      type="search"
      bind:value={query}
      placeholder="Find a setting…"
      aria-label="Find a setting"
      class="min-h-11 min-w-48 flex-1 rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
             px-3 text-sm focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
    />
    <label class="flex items-center gap-2 text-sm" class:opacity-40={query}>
      <input type="checkbox" bind:checked={onlyChanged} disabled={!!query} class="size-4" />
      Only what this file changes
    </label>
  </div>
  <p class="-mt-4 text-xs text-[var(--ink-dim)]">
    {#if needle && !byName}
      Nothing is <em>named</em> like that, so these are the settings whose description mentions it.
    {:else}
      Values are what the loader resolved, so machine settings and defaults show through. Saving
      writes only what this file changes; <span class="font-mono">Clear</span> takes a setting back
      out of it.
    {/if}
  </p>

  <section>
    <h3 class="mb-2 text-sm font-semibold tracking-wide uppercase">Scenes</h3>
    {#if scenes.length === 0}
      <p class="text-sm text-[var(--ink-dim)]">
        This configuration declares no scenes, so a run of it would have nothing to play.
      </p>
    {/if}
    <div class="space-y-4">
      {#each scenes as row (row.index)}
        {@const doc = docs.sceneType(row.scene.type)}
        <article
          class="rounded-lg border p-3
                 {dragOverIndex === row.index ? 'border-[var(--accent)]' : 'border-[var(--edge)]'}"
          ondragover={(e) => {
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
            dragOverIndex = row.index;
          }}
          ondragleave={() => (dragOverIndex = null)}
          ondrop={(e) => void dropUrl(row.index, e)}
        >
          <header class="mb-2 flex flex-wrap items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <h4 class="text-sm font-medium">
                <span class="text-[var(--ink-dim)]">{row.index + 1}.</span>
                <span class="font-mono">{row.scene.type}</span>
                {#if row.scene.name}
                  <span class="text-[var(--ink-dim)]">— {row.scene.name}</span>
                {/if}
              </h4>
              {#if doc?.help}
                <p class="mt-0.5 text-xs text-[var(--ink-dim)]">{doc.help}</p>
              {/if}
              {#if uploadingIndex === row.index}
                <div class="mt-1 flex items-center gap-2">
                  <progress
                    class="h-1.5 flex-1 accent-[var(--accent)]"
                    value={uploadComputable ? uploadLoaded : undefined}
                    max={uploadComputable ? uploadTotal : undefined}
                  ></progress>
                  <span class="shrink-0 text-xs text-[var(--ink-dim)]">
                    {uploadLabel(uploadingName, uploadLoaded, uploadTotal, uploadComputable)}
                  </span>
                  <button
                    type="button"
                    class="{chip} shrink-0"
                    disabled={!uploadAbort}
                    onclick={cancelUpload}
                  >
                    Cancel
                  </button>
                </div>
              {/if}
            </div>
            {#if !readOnly}
              <div class="flex gap-1">
                {#each moveDirections(row.index, scenes.length) as move (move.delta)}
                  <button
                    class={chip}
                    aria-label={move.label}
                    disabled={busy || structuralBlocked || move.atEdge}
                    title={structuralBlocked
                      ? "Save or discard the staged edits first — reordering renumbers the rest"
                      : move.label}
                    onclick={() =>
                      void structural(() => api.moveScene(path, row.index, row.index + move.delta))}
                  >
                    {move.symbol}
                  </button>
                {/each}
                <button
                  class={chip}
                  disabled={busy || structuralBlocked}
                  title={structuralBlocked
                    ? "Save or discard the staged edits first — adding a scene renumbers the rest"
                    : "Add a copy of this scene straight after it"}
                  onclick={() =>
                    void structural(() => api.addScene(path, { copy: row.index, after: row.index }))}
                >
                  Duplicate
                </button>
                <button
                  class={chip}
                  disabled={busy || structuralBlocked || scenes.length < 2}
                  title={scenes.length < 2
                    ? "A show needs a scene to play"
                    : "Remove this scene from the file"}
                  onclick={() => void structural(() => api.removeScene(path, row.index))}
                >
                  Remove
                </button>
              </div>
            {/if}
          </header>

          {#each row.fields as field (field.name)}
            {@const fd = docs.sceneField(row.scene.type, field.name)}
            {@const key = sceneKey(row.index, field.name)}
            {@const edit = { scene: row.index, field: field.name }}
            <FieldRow
              name={field.name}
              value={shownValue(field, key)}
              baseline={field.baseline}
              changed={!field.is_default}
              dirty={!!pending[key]}
              error={invalid[key] ?? ""}
              editable={!readOnly && field.name !== "type"}
              locked={field.name === "type"
                ? "A scene's type decides what its other fields mean, so changing it rewrites the block — edit this file as source."
                : ""}
              help={fd?.help ?? ""}
              type={fd?.type ?? ""}
              choices={fd?.choices ?? []}
              vocabulary={fd?.vocabulary ?? ""}
              palette={docs.palette}
              options={fd?.vocabulary === "media" ? mediaOptions(doc, row.index) : []}
              truncated={fd?.vocabulary === "media" && mediaTruncated(doc, row.index)}
              onsearch={fd?.vocabulary === "media"
                ? (q) => searchScene(doc, row.index, q)
                : undefined}
              live={fd?.apply === "live"}
              onedit={(v, e) => stage(key, edit, field, v, e)}
              onclear={() => clear(key, edit, field)}
              onrevert={() => revert(key)}
              onupload={fd?.vocabulary === "media"
                ? (file) => void uploadFile(row.index, field.name, file)
                : undefined}
            />
          {/each}

          {#each row.scene.overlays as overlay, j (j)}
            {@const kind = overlayType(overlay)}
            {@const od = docs.overlay(kind)}
            <!-- Shown whole rather than filtered, and never edited here: an
                 overlay list is replaced wholesale or not at all, which is the
                 text editor's job. -->
            <div class="mt-3 rounded-md bg-[var(--panel-alt)] p-2">
              <p class="font-mono text-xs">overlay: {kind}</p>
              {#if od?.help}
                <p class="mt-0.5 mb-1 text-xs text-[var(--ink-dim)]">{od.help}</p>
              {/if}
              {#each overlayEntries(overlay) as [key, value] (key)}
                {@const pd = docs.overlayParam(kind, key)}
                <FieldRow name={key} {value} help={pd?.help ?? ""} type={pd?.type ?? ""} />
              {/each}
            </div>
          {/each}
        </article>
      {/each}
    </div>

    {#if !readOnly}
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <label class="sr-only" for="new-scene-type">Type of scene to add</label>
        <select
          id="new-scene-type"
          bind:value={newType}
          disabled={busy || structuralBlocked}
          class="min-h-11 rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)] px-2 py-1
                 font-mono text-sm disabled:opacity-40
                 focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
        >
          {#each docs.sceneTypes as st (st.name)}
            <option value={st.name}>{st.name}</option>
          {/each}
        </select>
        <Button
          disabled={busy || structuralBlocked}
          onclick={() => void structural(() => api.addScene(path, { type: newType }))}
        >
          Add scene
        </Button>
        {#if structuralBlocked}
          <span class="text-xs text-c64-yellow">
            Save or discard the staged edits first — adding a scene renumbers the rest.
          </span>
        {/if}
      </div>
    {/if}
  </section>

  <section>
    <h3 class="mb-2 text-sm font-semibold tracking-wide uppercase">Settings</h3>
    {#if sections.length === 0}
      <p class="text-sm text-[var(--ink-dim)]">
        {#if query}
          No setting is named like that.
        {:else}
          Every setting is at its default — this configuration is its scenes and nothing else.
        {/if}
      </p>
    {/if}
    <div class="space-y-4">
      {#each sections as row (row.section.name)}
        {@const doc = docs.section(row.section.name)}
        <article>
          <h4 class="font-mono text-sm font-medium">[{row.section.name}]</h4>
          {#if doc?.help}
            <p class="mt-0.5 mb-1 text-xs text-[var(--ink-dim)]">{doc.help}</p>
          {/if}
          {#each row.fields as field (field.name)}
            {@const fd = docs.field(row.section.name, field.name)}
            {@const key = sectionKey(row.section.name, field.name)}
            {@const edit = { section: row.section.name, field: field.name }}
            <FieldRow
              name={field.name}
              value={shownValue(field, key)}
              baseline={field.baseline}
              changed={!field.is_default}
              dirty={!!pending[key]}
              error={invalid[key] ?? ""}
              editable={!readOnly}
              help={fd?.help ?? ""}
              type={fd?.type ?? ""}
              choices={fd?.choices ?? []}
              vocabulary={fd?.vocabulary ?? ""}
              palette={docs.palette}
              live={fd?.apply === "live"}
              onedit={(v, e) => stage(key, edit, field, v, e)}
              onclear={() => clear(key, edit, field)}
              onrevert={() => revert(key)}
            />
          {/each}
        </article>
      {/each}
    </div>
  </section>

  {#if readOnly}
    <p class="text-sm text-[var(--ink-dim)]">
      This console holds a read-only token, so the settings are shown but cannot be written.
    </p>
  {:else}
    <div
      class="sticky bottom-0 -mx-5 mt-2 flex flex-wrap items-center gap-2 border-t
             border-[var(--edge)] bg-[var(--panel)] px-5 py-3"
    >
      <Button variant="primary" disabled={busy || blocked || edits.length === 0} onclick={save}>
        {edits.length === 1 ? "Save 1 change" : `Save ${edits.length} changes`}
      </Button>
      <Button disabled={busy || edits.length === 0} onclick={discard}>Discard</Button>
      {#if blocked}
        <span class="text-xs text-c64-red">Something typed isn't a value yet.</span>
      {:else if restart.length}
        <span class="text-xs text-c64-yellow">
          unsaved · {restart.map((s) => `[${s}]`).join(", ")} will need a restart
        </span>
      {:else if edits.length}
        <span class="text-xs text-c64-yellow">unsaved changes</span>
      {/if}
    </div>
  {/if}

  {#if saved}
    <div class="rounded-lg border border-c64-green/50 px-3 py-2 text-sm text-c64-green">
      <p>{saved}</p>
      <MediaWarnings {warnings} heading="It is saved, but:" />
    </div>
  {/if}

  {#if problem}
    <p class="rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">{problem}</p>
  {/if}

  {#if report}
    <div class="rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">
      <p>{report.error ?? "This configuration would not load."}</p>
      {#if report.messages.length}
        <ul class="mt-1 list-disc pl-5 font-mono text-xs">
          {#each report.messages as message, i (i)}
            <li>{message}</li>
          {/each}
        </ul>
      {/if}
      <Diagnostics diagnostics={report.diagnostics} />
      <LayerBlame layers={report.layers} />
      <p class="mt-1 text-xs text-[var(--ink-dim)]">
        The file is untouched and the changes are still staged.
      </p>
    </div>
  {/if}
</div>
