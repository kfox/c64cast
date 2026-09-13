import { mount } from "svelte";

import { probeSetup, SETUP_PAGE_PATH, type SetupState } from "$lib/setup";
import App from "./App.svelte";
import Setup from "$lib/screens/Setup.svelte";
import "./app.css";

const found = document.getElementById("app");
if (!found) throw new Error("no #app element to mount into");
// Bound again because TypeScript does not carry the narrowing above into the
// callback below, and `mount` wants a non-null element.
const target: HTMLElement = found;

/** Mount the console, or the first-run form when the host is asking for one. */
function start(pending: SetupState | null): void {
  if (!pending) {
    mount(App, { target });
    return;
  }
  // A real path: no server route claims the segment, so the shell's catch-all
  // answers it, and `setup_gate.py` names it reachable without a token.
  if (window.location.pathname !== SETUP_PAGE_PATH) {
    window.history.replaceState({}, "", SETUP_PAGE_PATH);
  }
  mount(Setup, { target, props: { setup: pending } });
}

// One unauthenticated probe before anything mounts: while the appliance setup
// window is open (`setup_gate.py`) every other route answers 503. Anything
// other than a pending setup — including the 401 an ordinary host answers, and
// no answer at all — mounts the console.
probeSetup()
  .catch(() => null)
  .then(start);
