import { api } from "./api";
import type { Console } from "./console.svelte";

/** Start `ref` (or switch to it if a show is already running), and arm the
 *  shell to jump to Live once it actually comes up.
 *
 * The one place this happens, so every launch surface — the Session screen's
 * own Start/Switch buttons, a favorite's quick-launch, a double-click in the
 * config list — behaves the same way and the shell only has to watch one flag.
 * Left set on failure is wrong (there is nothing to jump to), so a caller that
 * shows its own error only needs to catch and report; this always clears it
 * first. */
export async function launch(host: Console, ref: string): Promise<void> {
  host.expectingStart = true;
  try {
    await (host.session?.state === "running" ? api.switch(ref) : api.start(ref));
  } catch (e) {
    host.expectingStart = false;
    throw e;
  }
}
