<script lang="ts">
  import { ApiError, api, reportOf } from "$lib/api";
  import Button from "$lib/components/Button.svelte";
  import Diagnostics from "$lib/components/Diagnostics.svelte";
  import LayerBlame from "$lib/components/LayerBlame.svelte";
  import MediaWarnings from "$lib/components/MediaWarnings.svelte";
  import type { ConfigWritten, ValidationReport } from "$lib/types";

  interface Props {
    /** The config ref being edited. Also the directory a dry-run validation
     *  happens in, which is why the check needs it and not just the text. */
    path: string;
    value: string;
    /** What is on disk, so "revert" and "dirty" mean something exact. */
    baseline: string;
    readOnly: boolean;
    onchange: (text: string) => void;
    onsaved: (written: ConfigWritten) => void;
  }

  let { path, value, baseline, readOnly, onchange, onsaved }: Props = $props();

  let report = $state<ValidationReport | null>(null);
  let problem = $state("");
  let busy = $state(false);
  let saved = $state("");

  const dirty = $derived(value !== baseline);

  function clear(): void {
    report = null;
    problem = "";
    saved = "";
  }

  async function act(fn: () => Promise<void>): Promise<void> {
    clear();
    busy = true;
    try {
      await fn();
    } catch (e) {
      // A refused save answers 422 with the whole validation report, which is
      // the same shape the check returns — so show it the same way rather than
      // reducing the loader's diagnostics to one line.
      const refused = reportOf(e);
      if (refused) report = refused;
      else if (e instanceof ApiError) problem = e.message;
      else problem = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  const check = () =>
    act(async () => {
      report = await api.checkConfig(path, value);
    });

  const save = () =>
    act(async () => {
      const written = await api.saveConfig(path, value);
      saved = written.backup
        ? `Saved ${written.bytes} bytes. The previous version is in ${written.backup}.`
        : `Saved ${written.bytes} bytes.`;
      onsaved(written);
    });
</script>

<div class="space-y-3">
  <textarea
    {value}
    readonly={readOnly}
    spellcheck="false"
    autocapitalize="off"
    oninput={(e) => onchange(e.currentTarget.value)}
    aria-label="Configuration source"
    class="h-96 w-full resize-y rounded-lg border border-[var(--edge)] bg-[var(--panel-alt)]
           p-3 font-mono text-xs leading-relaxed whitespace-pre
           focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
  ></textarea>

  {#if readOnly}
    <p class="text-sm text-[var(--ink-dim)]">
      This console holds a read-only token, so the file is shown but cannot be written.
    </p>
  {:else}
    <div class="flex flex-wrap items-center gap-2">
      <Button variant="primary" disabled={busy || !dirty} onclick={save}>Save</Button>
      <Button disabled={busy} onclick={check}>Check</Button>
      <Button
        disabled={busy || !dirty}
        onclick={() => {
          clear();
          onchange(baseline);
        }}
      >
        Revert
      </Button>
      {#if dirty}
        <span class="text-xs text-c64-yellow">unsaved changes</span>
      {/if}
    </div>
  {/if}

  {#if saved}
    <p class="rounded-lg border border-c64-green/50 px-3 py-2 text-sm text-c64-green">{saved}</p>
  {/if}

  {#if problem}
    <p class="rounded-lg border border-c64-red/50 px-3 py-2 text-sm text-c64-red">{problem}</p>
  {/if}

  {#if report}
    <div
      class="rounded-lg border px-3 py-2 text-sm
             {report.ok ? 'border-c64-green/50 text-c64-green' : 'border-c64-red/50 text-c64-red'}"
    >
      <p>{report.ok ? "This configuration loads and validates." : report.error}</p>
      {#if report.messages.length}
        <ul class="mt-1 list-disc pl-5 font-mono text-xs">
          {#each report.messages as message, i (i)}
            <li>{message}</li>
          {/each}
        </ul>
      {/if}
      {#if report.unknown_keys.length}
        <ul class="mt-1 list-disc pl-5 text-xs text-c64-yellow">
          {#each report.unknown_keys as key, i (i)}
            <li>
              <span class="font-mono">[{key.section}] {key.key}</span>
              {key.hint ? `— ${key.hint}` : "is not a key c64cast knows."}
            </li>
          {/each}
        </ul>
      {/if}
      <MediaWarnings warnings={report.warnings} />
      <Diagnostics diagnostics={report.diagnostics} />
      <LayerBlame layers={report.layers} />
    </div>
  {/if}
</div>
