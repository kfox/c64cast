<script lang="ts">
  import { ApiError, api, reportOf } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import FieldRow from "$lib/components/FieldRow.svelte";
  import LayerBlame from "$lib/components/LayerBlame.svelte";
  import MediaWarnings from "$lib/components/MediaWarnings.svelte";
  import type { DocIndex } from "$lib/introspect";
  import { kindsForScene, pickerOptions, urlFromDrop } from "$lib/mediaPickerLogic";
  import type {
    ConfigEdit,
    ConfigForm,
    ConfigWritten,
    FormField,
    FormScene,
    FormSection,
    MediaEntry,
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
    /** Edits typed but not saved, held by the screen so that clicking another
     *  file to compare against doesn't discard them. Keyed by row. */
    pending: Record<string, ConfigEdit>;
    onpending: (next: Record<string, ConfigEdit>) => void;
    /** `restart` names the sections a reload will *not* pick up, so the screen
     *  can stop offering a reload as if it were enough. Empty on a save that a
     *  reload covers in full — which includes every structural change, since
     *  adding or removing a scene is exactly what a reload is for. */
    onsaved: (written: ConfigWritten, restart: string[]) => void;
    /** Media kind -> what's browsable there (`Config.svelte` fetches one
     *  listing per kind any loaded scene type actually uses). Absent kinds
     *  just render an empty datalist — a picker with nothing to offer is a
     *  plain text box, which is exactly the fallback. */
    media?: Record<string, MediaEntry[]>;
  }

  let { form, docs, path, readOnly, pending, onpending, onsaved, media = {} }: Props = $props();

  /** The datalist options a scene type's `file =` field offers: the union of
   *  every media kind it browses, deduplicated. */
  function mediaOptions(doc: SceneTypeDoc | undefined): string[] {
    const entries = kindsForScene(doc).flatMap((kind) => media[kind] ?? []);
    return [...new Set(pickerOptions(entries))];
  }

  /** Hide every field still sitting at its baseline. On by default: a config
   *  has 167 settable fields and a show file names a dozen of them, and the
   *  dozen is the question being asked. */
  let onlyChanged = $state(true);
  let query = $state("");
  let report = $state<ValidationReport | null>(null);
  let problem = $state("");
  let saved = $state("");
  let busy = $state(false);

  // Half-typed values, kept here rather than beside the edits: a number that
  // isn't one yet is a state of this screen, not something worth carrying to
  // another file and back.
  let invalid = $state<Record<string, string>>({});
  // Carried out of the last save so a green "Saved" can't stand alone over a
  // config that names media this host hasn't got.
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

  /** A row's identity. The wire shape names a section *or* a scene index, and
   *  so does this — one string, so a lookup never has to reconstruct which. */
  const sectionKey = (section: string, field: string) => `s:${section}.${field}`;
  const sceneKey = (index: number, field: string) => `n:${index}.${field}`;

  const needle = $derived(query.trim().toLowerCase());

  /** Whether *any* field is named like the query. Names are searched first and
   *  alone, because matching help text on "color" pulls in everything that
   *  mentions color — but a reader who does not know a setting is called
   *  `cell_strategy` has no way in at all, so a query that names nothing falls
   *  through to the descriptions and the form says that is what happened. */
  const byName = $derived(
    needle !== "" &&
      [
        ...form.sections.flatMap((s) => s.fields.map((f) => f.name)),
        ...form.scenes.flatMap((s) => s.fields.map((f) => f.name)),
      ].some((name) => name.toLowerCase().includes(needle)),
  );

  function shown(fields: FormField[], key: (f: FormField) => string, help: HelpOf): FormField[] {
    return fields.filter((f) => {
      // An unsaved edit is never hidden by a filter — losing sight of one is
      // how it gets saved by accident or lost by surprise.
      if (pending[key(f)]) return true;
      // Searching is asking for a field, which is the one move the "only what
      // this file changes" filter would defeat.
      if (needle) return matches(f.name) || (!byName && help(f.name).toLowerCase().includes(needle));
      return !onlyChanged || !f.is_default;
    });
  }

  type HelpOf = (field: string) => string;

  function matches(name: string): boolean {
    return name.toLowerCase().includes(needle);
  }

  // A section with nothing to say disappears rather than leaving an empty
  // heading — with the filter on, that is most of them.
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

  // Scenes always show: they are what the file is *for*, and one with every
  // field at its default is still a scene the playlist will run.
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
   *  cleared row shows what it will fall back to, which is the whole point of
   *  clearing it. */
  function shownValue(field: FormField, key: string): unknown {
    const edit = pending[key];
    if (!edit) return field.value;
    return edit.reset ? field.baseline : edit.value;
  }

  function stage(key: string, edit: ConfigEdit, field: FormField, value: unknown, error: string): void {
    invalid = { ...invalid, [key]: error };
    if (error) return;
    // Typing the stored value back is not an edit. Compared as JSON because a
    // list or a table is a value here like any other.
    const same = JSON.stringify(value) === JSON.stringify(field.value);
    onpending(same ? without(key) : { ...pending, [key]: { ...edit, value } });
  }

  /** Stop setting the field here. On a row the file never set, that is the
   *  same as dropping the edit — there is nothing on disk to reset. */
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

  /** What it takes to *see* the change that was just saved — which is the
   *  question actually being asked at the moment of saving, and the one the
   *  console used to answer with a count and nothing else. */
  function applies(count: number, held: number, sections: string[]): string {
    const named = sections.map((s) => `[${s}]`).join(", ");
    const verb = sections.length === 1 ? "needs" : "need";
    if (held === 0) return count === 1 ? "It applies on a reload." : "They apply on a reload.";
    if (held === count) return `${named} ${verb} the session restarted.`;
    return `${named} ${verb} the session restarted; the rest apply on a reload.`;
  }

  /** Which scene type a new blank scene gets. The options come from the host's
   *  own list rather than a copy kept here. */
  let newType = $state("video");

  const chip = `min-h-9 rounded-md border border-[var(--edge)] px-2 text-xs
                text-[var(--ink-dim)] hover:text-[var(--ink)] disabled:opacity-40`;

  /** Adding or removing a scene renumbers the ones after it, and every staged
   *  edit is keyed by index — so the two cannot be in flight at once. Refusing
   *  is better than renumbering the staged edits, which would silently move an
   *  unsaved change onto a different scene. */
  const structuralBlocked = $derived(edits.length > 0);

  async function structural(act: () => Promise<ConfigWritten>): Promise<void> {
    report = null;
    problem = "";
    saved = "";
    warnings = [];
    busy = true;
    try {
      const written = await act();
      saved = `Saved. ${written.backup ? `The previous version is in ${written.backup}.` : ""}`;
      warnings = written.warnings ?? [];
      // No sections held back: a scene list is exactly what a reload re-reads.
      onsaved(written, []);
    } catch (e) {
      const refused = reportOf(e);
      if (refused) report = refused;
      else if (e instanceof ApiError) problem = e.message;
      else problem = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  // Highlighted while a drag hovers a scene card; `null` the rest of the time.
  let dragOverIndex = $state<number | null>(null);

  /** Dropping a **URL** onto a scene sets its `file =` field directly — no
   *  upload path exists, so anything else (a real file from the desktop) is
   *  ignored with a hint rather than silently doing nothing. Saved as its own
   *  immediate patch, the same way `structural()` already handles add/remove,
   *  because a drop isn't part of the staged-edit flow a keystroke goes
   *  through. */
  async function dropUrl(index: number, event: DragEvent): Promise<void> {
    event.preventDefault();
    dragOverIndex = null;
    if (readOnly || busy || structuralBlocked) return;
    const dt = event.dataTransfer;
    const url = urlFromDrop({
      "text/uri-list": dt?.getData("text/uri-list") ?? "",
      "text/plain": dt?.getData("text/plain") ?? "",
    });
    if (!url) {
      problem = "Drop a URL to set a scene's file — uploading a file from the desktop isn't wired up yet.";
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
    // Read before the save: `onsaved` re-reads the file, which clears the
    // staged edits these were derived from.
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
      // shape the text editor's Check returns, shown the same way rather than
      // reduced to one line. The edits stay staged: the file is untouched, so
      // what is on screen is still what the user meant to write.
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
            </div>
            {#if !readOnly}
              <div class="flex gap-1">
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
              options={fd?.vocabulary === "media" ? mediaOptions(doc) : []}
              live={fd?.apply === "live"}
              onedit={(v, e) => stage(key, edit, field, v, e)}
              onclear={() => clear(key, edit, field)}
              onrevert={() => revert(key)}
            />
          {/each}

          {#each row.scene.overlays as overlay, j (j)}
            {@const kind = overlayType(overlay)}
            {@const od = docs.overlay(kind)}
            <!-- Overlays are shown whole rather than filtered: an overlay only
                 exists in a config because somebody asked for it, so every key
                 in one is a deliberate answer. They are also the one part of a
                 scene the form does not edit — an overlay list is replaced
                 wholesale or not at all, which is the text editor's job. -->
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
      <!-- "Add another clip to this show" is the most common structural edit
           there is, and it was the one that still meant opening the source. -->
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
    <!-- Sticky: the form is longer than a screen and the save belongs where
         the hands are, not at the end of a scroll. -->
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
      <LayerBlame layers={report.layers} />
      <p class="mt-1 text-xs text-[var(--ink-dim)]">
        The file is untouched and the changes are still staged.
      </p>
    </div>
  {/if}
</div>
