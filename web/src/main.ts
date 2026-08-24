import { mount } from "svelte";

import { probeSetup, SETUP_PAGE_PATH, type SetupState } from "$lib/setup";
import App from "./App.svelte";
import Setup from "$lib/screens/Setup.svelte";
import "./app.css";

const found = document.getElementById("app");
if (!found) throw new Error("no #app element to mount into");
// Bound again after the check: `mount` wants a non-null element, and TypeScript
// does not carry the narrowing above into the callback below.
const target: HTMLElement = found;

/** Mount the console, or the first-run form when the host is asking for one. */
function start(pending: SetupState | null): void {
  if (!pending) {
    mount(App, { target });
    return;
  }
  // Whatever was bookmarked is unreachable while setup is pending, and the
  // form is a page somebody may reload — so put it in the address bar. It is
  // a real path: no server route claims the segment, so the shell's catch-all
  // answers it, and `setup_gate.py` names it as reachable without a token.
  if (window.location.pathname !== SETUP_PAGE_PATH) {
    window.history.replaceState({}, "", SETUP_PAGE_PATH);
  }
  mount(Setup, { target, props: { setup: pending } });
}

// One unauthenticated probe before anything mounts, and the only way the shell
// can know: while the appliance setup window is open (`setup_gate.py`) every
// other route answers 503, so a console that mounted first would come up with
// nothing but errors. Anything else — the 401 an ordinary host answers, or no
// answer at all because it is still coming up — is the console.
probeSetup()
  .catch(() => null)
  .then(start);
