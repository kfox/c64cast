/** Shapes the daemon sends. Hand-written rather than generated: the API is
 *  small, and a generator would be a second thing to keep in sync with the
 *  Python that is already the source of truth. `svelte-check` in `npm run
 *  build` is what catches a screen reading a field that isn't here. */

export type SessionState = "idle" | "starting" | "running" | "stopping" | "error";

/** `SessionStatus.as_dict()` in c64cast/app/serve.py, plus the log cursor
 *  web_api adds to it. */
export interface SessionStatus {
  state: SessionState;
  generation: number;
  config_path: string;
  systems: string[];
  last_error: string | null;
  hardware_wait_s: number;
  log_seq: number;
}

/** One record from `SessionLogBuffer`. */
export interface LogLine {
  seq: number;
  t: number;
  level: string;
  name: string;
  message: string;
  generation: number;
}

/** One `.toml` under one of `[web].config_roots`. `path` is the ref the API
 *  addresses it by — `<root-label>/<relative>`, never a filesystem path. */
export interface ConfigFile {
  path: string;
  root: string;
  name: string;
  size: number;
  mtime: number;
}

export interface ConfigRoot {
  label: string;
  path: string;
}

export interface ConfigIndex {
  roots: ConfigRoot[];
  files: ConfigFile[];
  truncated: boolean;
}

/** `"full"` may write; `"viewer"` may only watch. Null when the host runs
 *  without authentication, which `[web]` never does. */
export type Role = "full" | "viewer" | null;

/** A frame off `/api/ws`: the performance console's payload with the
 *  supervisor bolted on. Only the keys this app reads are named; the rest
 *  arrive untouched for the screens that will. */
export interface StateFrame {
  role?: Role;
  session?: SessionStatus;
  log?: LogLine[];
  [key: string]: unknown;
}
