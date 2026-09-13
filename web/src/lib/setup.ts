/**
 * The appliance first-run form's client (`c64cast/control/setup_api.py`).
 *
 * Its own transport rather than `api.ts`'s, which rides a token this browser
 * does not have yet: while the setup window is open `setup_gate.py` answers
 * `503` to everything but this route, the form's own page and `/assets`.
 */

/** Where the shell puts the form in the address bar. Server-side twin:
 *  `SETUP_PAGE_PATH` in `c64cast/control/setup_gate.py`, which is what makes
 *  the path reachable with no token — so the two have to agree. */
export const SETUP_PAGE_PATH = "/setup";

const SETUP_PATH = "/api/setup";

/** How often to re-ask whether the host has finished restarting, and how long
 *  to keep asking. A restart is a rebuilt app on an already-warm process. */
const RESTART_POLL_MS = 400;
const RESTART_TIMEOUT_MS = 30_000;

export interface SetupState {
  pending: boolean;
  /** Whether a token typed here would actually take effect. False when the
   *  host's token is named by its configuration rather than generated;
   *  `setup_api.py` refuses one in that case. */
  token_settable: boolean;
}

export interface SetupSubmission {
  connection: string;
  /** Empty keeps whatever token the host already has. */
  token: string;
}

/**
 * The host's setup state, or `null` when it has none to report — an ordinary
 * console answers `401` here, because the route stops existing the moment
 * setup completes. Throws only when the host could not be reached at all,
 * which is what lets `waitForRestart` tell "down" from "back up".
 */
export async function probeSetup(): Promise<SetupState | null> {
  const response = await fetch(SETUP_PATH, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) return null;
  const state = (await response.json()) as SetupState;
  return state.pending ? state : null;
}

/** Submit the form. Resolves to the one-shot login URL that gets this browser
 *  into the console it just configured; throws with the host's own message
 *  for anything it refused. */
export async function submitSetup(
  submission: SetupSubmission,
): Promise<string> {
  const response = await fetch(SETUP_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(submission),
  });
  const body = (await response.json().catch(() => null)) as {
    error?: string;
    login_url?: string;
  } | null;
  if (!response.ok || !body?.login_url) {
    throw new Error(body?.error || `${response.status} ${response.statusText}`);
  }
  return body.login_url;
}

/** Wait for the host to come back on the same port with setup behind it.
 *  Resolves `false` if it never does, so the page can offer the link rather
 *  than navigating into a hole. */
export async function waitForRestart(
  sleep: (ms: number) => Promise<void> = defaultSleep,
): Promise<boolean> {
  const deadline = Date.now() + RESTART_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(RESTART_POLL_MS);
    try {
      if ((await probeSetup()) === null) return true;
    } catch {
      // The listening socket is down between the two apps — expected, and why
      // this polls rather than navigating straight away.
    }
  }
  return false;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
