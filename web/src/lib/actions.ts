import { api } from "./api";
import type { Console } from "./console.svelte";
import type { LibraryState, ValidationReport } from "./types";

/** Thrown by `launch` when the pre-flight refuses `ref` before a start or
 *  switch is ever attempted — carries the whole report, the same shape a
 *  refused save answers with, so a catcher can show everything wrong with the
 *  config rather than reducing it to one line. */
export class PreflightRefused extends Error {
  readonly report: ValidationReport;

  constructor(report: ValidationReport) {
    super(report.error ?? "This configuration will not run.");
    this.name = "PreflightRefused";
    this.report = report;
  }
}

/** Start `ref` (or switch to it if a show is already running), and arm the
 *  shell to jump to Live once it actually comes up.
 *
 *  Pre-flights `ref` first and throws `PreflightRefused` rather than claiming
 *  the transition; the arm flag is cleared again on any failure. */
export async function launch(host: Console, ref: string): Promise<void> {
  host.expectingStart = true;
  try {
    const report = await api.checkConfig(ref);
    if (!report.ok) throw new PreflightRefused(report);
    await (host.session?.state === "running" ? api.switch(ref) : api.start(ref));
  } catch (e) {
    host.expectingStart = false;
    throw e;
  }
}

/** Fetch the shared favorites/recents library. */
export const fetchLibrary = (): Promise<LibraryState> => api.library();

/** Toggle `ref`'s favorite state and fold the result into `library`. Returns
 *  `library` unchanged if it hasn't loaded yet. */
export async function withToggledFavorite(
  library: LibraryState | null,
  ref: string,
  on: boolean,
): Promise<LibraryState | null> {
  const { favorites } = await api.favorite(ref, on);
  return library ? { ...library, favorites } : library;
}
