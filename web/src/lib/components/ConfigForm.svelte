<script lang="ts">
  import FieldRow from "$lib/components/FieldRow.svelte";
  import type { DocIndex } from "$lib/introspect";
  import type { ConfigForm, FormField, FormScene, FormSection } from "$lib/types";

  interface Props {
    form: ConfigForm;
    docs: DocIndex;
    /** Hide every field still sitting at its default. On by default: a config
     *  has 167 settable fields and a show file names a dozen of them, and the
     *  dozen is the question being asked. */
    onlyChanged: boolean;
  }

  let { form, docs, onlyChanged }: Props = $props();

  function shown(fields: FormField[]): FormField[] {
    return onlyChanged ? fields.filter((f) => !f.is_default) : fields;
  }

  // A section with nothing to say disappears rather than leaving an empty
  // heading — with the filter on, that is most of them.
  const sections = $derived(
    form.sections
      .map((s: FormSection) => ({ section: s, fields: shown(s.fields) }))
      .filter((row) => row.fields.length > 0),
  );

  // Scenes always show: they are what the file is *for*, and one with every
  // field at its default is still a scene the playlist will run.
  const scenes = $derived(
    form.scenes.map((sc: FormScene) => ({ scene: sc, fields: shown(sc.fields) })),
  );

  function overlayEntries(overlay: unknown): [string, unknown][] {
    if (!overlay || typeof overlay !== "object") return [];
    return Object.entries(overlay as Record<string, unknown>).filter(([k]) => k !== "type");
  }

  function overlayType(overlay: unknown): string {
    const type = (overlay as { type?: unknown } | null)?.type;
    return typeof type === "string" ? type : "overlay";
  }
</script>

<div class="space-y-6">
  <section>
    <h3 class="mb-2 text-sm font-semibold tracking-wide uppercase">Scenes</h3>
    {#if scenes.length === 0}
      <p class="text-sm text-[var(--ink-dim)]">
        This configuration declares no scenes, so a run of it would have nothing to play.
      </p>
    {/if}
    <div class="space-y-4">
      {#each scenes as row, i (i)}
        {@const doc = docs.sceneType(row.scene.type)}
        <article class="rounded-lg border border-[var(--edge)] p-3">
          <header class="mb-2">
            <h4 class="text-sm font-medium">
              <span class="font-mono">{row.scene.type}</span>
              {#if row.scene.name}
                <span class="text-[var(--ink-dim)]">— {row.scene.name}</span>
              {/if}
            </h4>
            {#if doc?.help}
              <p class="mt-0.5 text-xs text-[var(--ink-dim)]">{doc.help}</p>
            {/if}
          </header>

          {#each row.fields as field (field.name)}
            {@const fd = docs.sceneField(row.scene.type, field.name)}
            <FieldRow
              name={field.name}
              value={field.value}
              changed={!field.is_default}
              help={fd?.help ?? ""}
              type={fd?.type ?? ""}
              choices={fd?.choices ?? []}
              live={fd?.apply === "live"}
            />
          {/each}

          {#each row.scene.overlays as overlay, j (j)}
            {@const kind = overlayType(overlay)}
            {@const od = docs.overlay(kind)}
            <!-- Overlays are shown whole rather than filtered: an overlay only
                 exists in a config because somebody asked for it, so every key
                 in one is a deliberate answer. -->
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
  </section>

  <section>
    <h3 class="mb-2 text-sm font-semibold tracking-wide uppercase">Settings</h3>
    {#if sections.length === 0}
      <p class="text-sm text-[var(--ink-dim)]">
        Every setting is at its default — this configuration is its scenes and nothing else.
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
            <FieldRow
              name={field.name}
              value={field.value}
              changed={!field.is_default}
              help={fd?.help ?? ""}
              type={fd?.type ?? ""}
              choices={fd?.choices ?? []}
              live={fd?.apply === "live"}
            />
          {/each}
        </article>
      {/each}
    </div>
  </section>
</div>
